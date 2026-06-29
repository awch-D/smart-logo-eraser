"""PerSAM-F 引擎：把"参考图 + 用户框选的 logo bbox + 目标图"转成"目标图上 logo 的精确像素 mask"。

设计：
- 模型懒加载（首次调用时才加载 MobileSAM ~38MB）。
- 参考图特征只编码一次（per template），结果缓存到内存。
- 目标图调用时一次性运行 PerSAM 流程（含 1 次定位 + 2 次级联精修）。
- 输入的 hint_box（来自 OpenCV 模板匹配）会作为兜底裁剪：mask 必须落在 hint_box ±margin 范围内，
  避免 PerSAM 偶尔把图里别的相似区域也分进去。
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path
from typing import Dict, Optional, Tuple

import cv2
import numpy as np
import torch
import torch.nn.functional as F

ROOT = Path(__file__).parent
VENDOR = ROOT / 'vendor' / 'personalize_sam'
if str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))

from per_segment_anything import sam_model_registry, SamPredictor  # noqa: E402

SAM_CKPT = VENDOR / 'weights' / 'mobile_sam.pt'
SAM_TYPE = 'vit_t'  # MobileSAM


def _pick_device() -> torch.device:
    if torch.backends.mps.is_available():
        return torch.device('mps')
    if torch.cuda.is_available():
        return torch.device('cuda')
    return torch.device('cpu')


def _make_ref_mask(image_shape, box) -> np.ndarray:
    H, W = image_shape[:2]
    m = np.zeros((H, W, 3), dtype=np.uint8)
    x0, y0, x1, y1 = box
    m[max(0, y0):min(H, y1), max(0, x0):min(W, x1)] = 255
    return m


def _point_selection(mask_sim: torch.Tensor, topk: int = 1):
    h_, w_ = mask_sim.shape
    topk_xy = mask_sim.flatten(0).topk(topk)[1]
    topk_x = (topk_xy // w_).unsqueeze(0)
    topk_y = (topk_xy - topk_x * w_)
    topk_xy = torch.cat((topk_y, topk_x), dim=0).permute(1, 0).cpu().numpy()
    topk_label = np.array([1] * topk)

    last_xy = mask_sim.flatten(0).topk(topk, largest=False)[1]
    last_x = (last_xy // w_).unsqueeze(0)
    last_y = (last_xy - last_x * w_)
    last_xy = torch.cat((last_y, last_x), dim=0).permute(1, 0).cpu().numpy()
    last_label = np.array([0] * topk)
    return topk_xy, topk_label, last_xy, last_label


class PerSamEngine:
    """单例：加载一次模型，复用预测器。"""

    _inst: Optional['PerSamEngine'] = None
    _inst_lock = threading.Lock()

    def __init__(self):
        self.device = _pick_device()
        print(f'[PerSAM] device={self.device}, loading {SAM_TYPE} ...')
        sam = sam_model_registry[SAM_TYPE](checkpoint=str(SAM_CKPT)).to(device=self.device)
        sam.eval()
        for p in sam.parameters():
            p.requires_grad = False
        self.predictor = SamPredictor(sam)
        # template_id -> (target_feat_cos, target_embedding)
        self._ref_cache: Dict[str, Tuple[torch.Tensor, torch.Tensor]] = {}
        self._call_lock = threading.Lock()
        print('[PerSAM] ready.')

    @classmethod
    def instance(cls) -> 'PerSamEngine':
        if cls._inst is None:
            with cls._inst_lock:
                if cls._inst is None:
                    cls._inst = cls()
        return cls._inst

    def encode_reference(self, key: str, ref_image_bgr: np.ndarray, ref_box) -> None:
        """对一个模板的参考图+box 计算并缓存特征。重复 key 直接复用。"""
        if key in self._ref_cache:
            return
        with self._call_lock:
            ref_rgb = cv2.cvtColor(ref_image_bgr, cv2.COLOR_BGR2RGB)
            ref_mask = _make_ref_mask(ref_rgb.shape, ref_box)
            ref_mask_t = self.predictor.set_image(ref_rgb, ref_mask)
            ref_feat = self.predictor.features.squeeze().permute(1, 2, 0)
            ref_mask_t = F.interpolate(ref_mask_t, size=ref_feat.shape[:2], mode='bilinear')
            ref_mask_t = ref_mask_t.squeeze()[0]
            target_feat = ref_feat[ref_mask_t > 0]
            if target_feat.numel() == 0:
                raise RuntimeError('参考 mask 内没有特征，参考 box 可能在图像外')
            target_embedding = target_feat.mean(0).unsqueeze(0)
            target_feat_cos = target_embedding / target_embedding.norm(dim=-1, keepdim=True)
            target_embedding_full = target_embedding.unsqueeze(0)
            # detach + clone 防止被后续推理污染
            self._ref_cache[key] = (
                target_feat_cos.detach().clone(),
                target_embedding_full.detach().clone(),
            )

    def segment(
        self,
        key: str,
        target_bgr: np.ndarray,
        hint_box: Optional[Tuple[int, int, int, int]] = None,
        hint_margin: int = 6,
        hint_margin_pct: float = 0.5,
    ) -> np.ndarray:
        """对一张目标图返回二值 mask (HxW bool)。

        hint_box 为可选的 OpenCV 模板匹配框；提供时 mask 会与扩展后的笼子求交集，
        把 PerSAM 偶尔吃到的远处误匹配剔除。
        笼子扩展量 = hint_margin 像素 + hint_margin_pct * 匹配框宽/高，
        以适配匹配框比 logo 实际偏小的情况。
        """
        if key not in self._ref_cache:
            raise RuntimeError(f'参考特征未注册: {key}')
        with self._call_lock:
            target_feat_cos, target_embedding = self._ref_cache[key]

            test_image = cv2.cvtColor(target_bgr, cv2.COLOR_BGR2RGB)
            self.predictor.set_image(test_image)
            test_feat = self.predictor.features.squeeze()

            C, h, w = test_feat.shape
            test_feat_n = test_feat / test_feat.norm(dim=0, keepdim=True)
            test_feat_n = test_feat_n.reshape(C, h * w)
            sim = target_feat_cos @ test_feat_n
            sim = sim.reshape(1, 1, h, w)
            sim = F.interpolate(sim, scale_factor=4, mode='bilinear')
            sim = self.predictor.model.postprocess_masks(
                sim,
                input_size=self.predictor.input_size,
                original_size=self.predictor.original_size,
            ).squeeze()

            tk_xy_i, tk_l_i, lst_xy_i, lst_l_i = _point_selection(sim, topk=1)
            topk_xy = np.concatenate([tk_xy_i, lst_xy_i], axis=0)
            topk_label = np.concatenate([tk_l_i, lst_l_i], axis=0)

            sim_norm = (sim - sim.mean()) / torch.std(sim)
            sim_norm = F.interpolate(sim_norm.unsqueeze(0).unsqueeze(0), size=(64, 64), mode='bilinear')
            attn_sim = sim_norm.sigmoid_().unsqueeze(0).flatten(3)

            masks, scores, logits, _ = self.predictor.predict(
                point_coords=topk_xy,
                point_labels=topk_label,
                multimask_output=False,
                attn_sim=attn_sim,
                target_embedding=target_embedding,
            )
            best_idx = 0

            masks, scores, logits, _ = self.predictor.predict(
                point_coords=topk_xy,
                point_labels=topk_label,
                mask_input=logits[best_idx:best_idx + 1, :, :],
                multimask_output=True,
            )
            best_idx = int(np.argmax(scores))

            y, x = np.nonzero(masks[best_idx])
            if len(x) > 0:
                input_box = np.array([x.min(), y.min(), x.max(), y.max()])
                masks, scores, logits, _ = self.predictor.predict(
                    point_coords=topk_xy,
                    point_labels=topk_label,
                    box=input_box[None, :],
                    mask_input=logits[best_idx:best_idx + 1, :, :],
                    multimask_output=True,
                )
                best_idx = int(np.argmax(scores))

            mask = masks[best_idx].astype(bool)

            # 用模板匹配框做"安全笼"：mask 只保留在 hint_box ± margin 之内
            if hint_box is not None:
                H, W = mask.shape
                hx0, hy0, hx1, hy1 = hint_box
                bw = max(1, hx1 - hx0)
                bh = max(1, hy1 - hy0)
                mx = int(hint_margin + bw * hint_margin_pct)
                my = int(hint_margin + bh * hint_margin_pct)
                cx0 = max(0, hx0 - mx)
                cy0 = max(0, hy0 - my)
                cx1 = min(W, hx1 + mx)
                cy1 = min(H, hy1 + my)
                cage = np.zeros_like(mask)
                cage[cy0:cy1, cx0:cx1] = True
                mask = mask & cage

            return mask

    def evict_reference(self, key: str) -> None:
        self._ref_cache.pop(key, None)
