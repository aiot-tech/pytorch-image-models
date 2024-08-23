"""
MambaOut models for image classification.
Some implementations are modified from:
timm (https://github.com/rwightman/pytorch-image-models),
MetaFormer (https://github.com/sail-sg/metaformer),
InceptionNeXt (https://github.com/sail-sg/inceptionnext)
"""
from functools import partial
from typing import Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from timm.models.layers import trunc_normal_, DropPath, LayerNorm
from .vision_transformer import LayerScale
from ._manipulate import checkpoint_seq
from timm.models.registry import register_model
from timm.data import IMAGENET_DEFAULT_MEAN, IMAGENET_DEFAULT_STD


class Stem(nn.Module):
    r""" Code modified from InternImage:
        https://github.com/OpenGVLab/InternImage
    """

    def __init__(
            self,
            in_chs=3,
            out_chs=96,
            mid_norm: bool = True,
            act_layer=nn.GELU,
            norm_layer=LayerNorm,
    ):
        super().__init__()
        self.conv1 = nn.Conv2d(
            in_chs,
            out_chs // 2,
            kernel_size=3,
            stride=2,
            padding=1
        )
        self.norm1 = norm_layer(out_chs // 2) if mid_norm else None
        self.act = act_layer()
        self.conv2 = nn.Conv2d(
            out_chs // 2,
            out_chs,
            kernel_size=3,
            stride=2,
            padding=1
        )
        self.norm2 = norm_layer(out_chs)

    def forward(self, x):
        x = self.conv1(x)
        if self.norm1 is not None:
            x = x.permute(0, 2, 3, 1)
            x = self.norm1(x)
            x = x.permute(0, 3, 1, 2)
        x = self.act(x)
        x = self.conv2(x)
        x = x.permute(0, 2, 3, 1)
        x = self.norm2(x)
        return x


class DownsampleNormFirst(nn.Module):

    def __init__(
            self,
            in_chs=96,
            out_chs=198,
            norm_layer=LayerNorm,
    ):
        super().__init__()
        self.norm = norm_layer(in_chs)
        self.conv = nn.Conv2d(
            in_chs,
            out_chs,
            kernel_size=3,
            stride=2,
            padding=1
        )

    def forward(self, x):
        x = self.norm(x)
        x = x.permute(0, 3, 1, 2)
        x = self.conv(x)
        x = x.permute(0, 2, 3, 1)
        return x


class Downsample(nn.Module):

    def __init__(
            self,
            in_chs=96,
            out_chs=198,
            norm_layer=LayerNorm,
    ):
        super().__init__()
        self.conv = nn.Conv2d(
            in_chs,
            out_chs,
            kernel_size=3,
            stride=2,
            padding=1
        )
        self.norm = norm_layer(out_chs)

    def forward(self, x):
        x = x.permute(0, 3, 1, 2)
        x = self.conv(x)
        x = x.permute(0, 2, 3, 1)
        x = self.norm(x)
        return x


class MlpHead(nn.Module):
    """ MLP classification head
    """

    def __init__(
            self,
            dim,
            num_classes=1000,
            act_layer=nn.GELU,
            mlp_ratio=4,
            norm_layer=LayerNorm,
            drop_rate=0.,
            bias=True,
    ):
        super().__init__()
        hidden_features = int(mlp_ratio * dim)
        self.fc1 = nn.Linear(dim, hidden_features, bias=bias)
        self.act = act_layer()
        self.norm = norm_layer(hidden_features)
        self.fc2 = nn.Linear(hidden_features, num_classes, bias=bias)
        self.head_dropout = nn.Dropout(drop_rate)

    def forward(self, x):
        x = self.fc1(x)
        x = self.act(x)
        x = self.norm(x)
        x = self.head_dropout(x)
        x = self.fc2(x)
        return x


class GatedConvBlock(nn.Module):
    r""" Our implementation of Gated CNN Block: https://arxiv.org/pdf/1612.08083
    Args:
        conv_ratio: control the number of channels to conduct depthwise convolution.
            Conduct convolution on partial channels can improve paraitcal efficiency.
            The idea of partial channels is from ShuffleNet V2 (https://arxiv.org/abs/1807.11164) and
            also used by InceptionNeXt (https://arxiv.org/abs/2303.16900) and FasterNet (https://arxiv.org/abs/2303.03667)
    """

    def __init__(
            self,
            dim,
            expansion_ratio=8 / 3,
            kernel_size=7,
            conv_ratio=1.0,
            ls_init_value=None,
            norm_layer=LayerNorm,
            act_layer=nn.GELU,
            drop_path=0.,
            **kwargs
    ):
        super().__init__()
        self.norm = norm_layer(dim)
        hidden = int(expansion_ratio * dim)
        self.fc1 = nn.Linear(dim, hidden * 2)
        self.act = act_layer()
        conv_channels = int(conv_ratio * dim)
        self.split_indices = (hidden, hidden - conv_channels, conv_channels)
        self.conv = nn.Conv2d(
            conv_channels,
            conv_channels,
            kernel_size=kernel_size,
            padding=kernel_size // 2,
            groups=conv_channels
        )
        self.fc2 = nn.Linear(hidden, dim)
        self.ls = LayerScale(dim) if ls_init_value is not None else nn.Identity()
        self.drop_path = DropPath(drop_path) if drop_path > 0. else nn.Identity()

    def forward(self, x):
        shortcut = x  # [B, H, W, C]
        x = self.norm(x)
        x = self.fc1(x)
        g, i, c = torch.split(x, self.split_indices, dim=-1)
        c = c.permute(0, 3, 1, 2)  # [B, H, W, C] -> [B, C, H, W]
        c = self.conv(c)
        c = c.permute(0, 2, 3, 1)  # [B, C, H, W] -> [B, H, W, C]
        x = self.fc2(self.act(g) * torch.cat((i, c), dim=-1))
        x = self.ls(x)
        x = self.drop_path(x)
        return x + shortcut


class MambaOutStage(nn.Module):

    def __init__(
            self,
            dim,
            dim_out: Optional[int] = None,
            depth: int = 4,
            expansion_ratio=8 / 3,
            kernel_size=7,
            conv_ratio=1.0,
            downsample: bool = False,
            ls_init_value: Optional[float] = None,
            norm_layer=LayerNorm,
            act_layer=nn.GELU,
            drop_path=0.,
    ):
        super().__init__()
        dim_out = dim_out or dim
        self.grad_checkpointing = False

        if downsample:
            self.downsample = Downsample(dim, dim_out, norm_layer=norm_layer)
        else:
            assert dim == dim_out
            self.downsample = nn.Identity()

        self.blocks = nn.Sequential(*[
            GatedConvBlock(
                dim=dim_out,
                expansion_ratio=expansion_ratio,
                kernel_size=kernel_size,
                conv_ratio=conv_ratio,
                ls_init_value=ls_init_value,
                norm_layer=norm_layer,
                act_layer=act_layer,
                drop_path=drop_path[j] if isinstance(drop_path, (list, tuple)) else drop_path,
            )
            for j in range(depth)
        ])

    def forward(self, x):
        x = self.downsample(x)
        if self.grad_checkpointing and not torch.jit.is_scripting():
            x = checkpoint_seq(self.blocks, x)
        else:
            x = self.blocks(x)
        return x


class MambaOut(nn.Module):
    r""" MetaFormer
        A PyTorch impl of : `MetaFormer Baselines for Vision`  -
          https://arxiv.org/abs/2210.13452

    Args:
        in_chans (int): Number of input image channels. Default: 3.
        num_classes (int): Number of classes for classification head. Default: 1000.
        depths (list or tuple): Number of blocks at each stage. Default: [3, 3, 9, 3].
        dims (int): Feature dimension at each stage. Default: [96, 192, 384, 576].
        downsample_layers: (list or tuple): Downsampling layers before each stage.
        drop_path_rate (float): Stochastic depth rate. Default: 0.
        output_norm: norm before classifier head. Default: partial(nn.LayerNorm, eps=1e-6).
        head_fn: classification head. Default: nn.Linear.
        head_dropout (float): dropout for MLP classifier. Default: 0.
    """

    def __init__(
            self,
            in_chans=3,
            num_classes=1000,
            depths=(3, 3, 9, 3),
            dims=(96, 192, 384, 576),
            norm_layer=LayerNorm,
            act_layer=nn.GELU,
            conv_ratio=1.0,
            kernel_size=7,
            ls_init_value=None,
            drop_path_rate=0.,
            drop_rate=0.,
            output_norm=LayerNorm,
            head_fn=MlpHead,
            **kwargs,
    ):
        super().__init__()
        self.num_classes = num_classes
        self.drop_rate = drop_rate
        if not isinstance(depths, (list, tuple)):
            depths = [depths]  # it means the model has only one stage
        if not isinstance(dims, (list, tuple)):
            dims = [dims]

        num_stage = len(depths)
        self.num_stage = num_stage

        self.stem = Stem(in_chans, dims[0], act_layer=act_layer, norm_layer=norm_layer)
        prev_dim = dims[0]
        dp_rates = [x.tolist() for x in torch.linspace(0, drop_path_rate, sum(depths)).split(depths)]
        self.stages = nn.ModuleList()
        cur = 0
        for i in range(num_stage):
            dim = dims[i]
            stage = MambaOutStage(
                dim=prev_dim,
                dim_out=dim,
                depth=depths[i],
                kernel_size=kernel_size,
                conv_ratio=conv_ratio,
                downsample=i > 0,
                ls_init_value=ls_init_value,
                norm_layer=norm_layer,
                act_layer=act_layer,
                drop_path=dp_rates[i],
            )
            self.stages.append(stage)
            prev_dim = dim
            cur += depths[i]

        self.norm = output_norm(prev_dim)

        self.head = head_fn(prev_dim, num_classes, drop_rate=drop_rate)

        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, (nn.Conv2d, nn.Linear)):
            trunc_normal_(m.weight, std=.02)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'norm'}

    def forward_features(self, x):
        x = self.stem(x)
        for s in self.stages:
            x = s(x)
        return x

    def forward_head(self, x):
        x = x.mean((1, 2))
        x = self.norm(x)
        x = self.head(x)
        return x

    def forward(self, x):
        x = self.forward_features(x)
        x = self.forward_head(x)
        return x


def _cfg(url='', **kwargs):
    return {
        'url': url,
        'num_classes': 1000, 'input_size': (3, 224, 224), 'pool_size': None,
        'crop_pct': 1.0, 'interpolation': 'bicubic',
        'mean': IMAGENET_DEFAULT_MEAN, 'std': IMAGENET_DEFAULT_STD, 'classifier': 'head',
        **kwargs
    }


default_cfgs = {
    'mambaout_femto': _cfg(
        url='https://github.com/yuweihao/MambaOut/releases/download/model/mambaout_femto.pth'),
    'mambaout_kobe': _cfg(
        url='https://github.com/yuweihao/MambaOut/releases/download/model/mambaout_kobe.pth'),
    'mambaout_tiny': _cfg(
        url='https://github.com/yuweihao/MambaOut/releases/download/model/mambaout_tiny.pth'),
    'mambaout_small': _cfg(
        url='https://github.com/yuweihao/MambaOut/releases/download/model/mambaout_small.pth'),
    'mambaout_base': _cfg(
        url='https://github.com/yuweihao/MambaOut/releases/download/model/mambaout_base.pth'),
    'mambaout_small_rw': _cfg(),
    'mambaout_base_rw': _cfg(),
}


# a series of MambaOut models
@register_model
def mambaout_femto(pretrained=False, **kwargs):
    model = MambaOut(
        depths=[3, 3, 9, 3],
        dims=[48, 96, 192, 288],
        **kwargs)
    model.default_cfg = default_cfgs['mambaout_femto']
    if pretrained:
        state_dict = torch.hub.load_state_dict_from_url(
            url=model.default_cfg['url'], map_location="cpu", check_hash=True)
        model.load_state_dict(state_dict)
    return model


# Kobe Memorial Version with 24 Gated CNN blocks
@register_model
def mambaout_kobe(pretrained=False, **kwargs):
    model = MambaOut(
        depths=[3, 3, 15, 3],
        dims=[48, 96, 192, 288],
        **kwargs)
    model.default_cfg = default_cfgs['mambaout_kobe']
    if pretrained:
        state_dict = torch.hub.load_state_dict_from_url(
            url=model.default_cfg['url'], map_location="cpu", check_hash=True)
        model.load_state_dict(state_dict)
    return model


@register_model
def mambaout_tiny(pretrained=False, **kwargs):
    model = MambaOut(
        depths=[3, 3, 9, 3],
        dims=[96, 192, 384, 576],
        **kwargs)
    model.default_cfg = default_cfgs['mambaout_tiny']
    if pretrained:
        state_dict = torch.hub.load_state_dict_from_url(
            url=model.default_cfg['url'], map_location="cpu", check_hash=True)
        model.load_state_dict(state_dict)
    return model


@register_model
def mambaout_small(pretrained=False, **kwargs):
    model = MambaOut(
        depths=[3, 4, 27, 3],
        dims=[96, 192, 384, 576],
        **kwargs)
    model.default_cfg = default_cfgs['mambaout_small']
    if pretrained:
        state_dict = torch.hub.load_state_dict_from_url(
            url=model.default_cfg['url'], map_location="cpu", check_hash=True)
        model.load_state_dict(state_dict)
    return model


@register_model
def mambaout_base(pretrained=False, **kwargs):
    model = MambaOut(
        depths=[3, 4, 27, 3],
        dims=[128, 256, 512, 768],
        **kwargs)
    model.default_cfg = default_cfgs['mambaout_base']
    if pretrained:
        state_dict = torch.hub.load_state_dict_from_url(
            url=model.default_cfg['url'], map_location="cpu", check_hash=True)
        model.load_state_dict(state_dict)
    return model


@register_model
def mambaout_small_rw(pretrained=False, **kwargs):
    model = MambaOut(
        depths=[3, 4, 27, 3],
        dims=[96, 192, 384, 576],
        ls_init_value=1e-6,
        **kwargs,
    )
    model.default_cfg = default_cfgs['mambaout_small']
    if pretrained:
        state_dict = torch.hub.load_state_dict_from_url(
            url=model.default_cfg['url'], map_location="cpu", check_hash=True)
        model.load_state_dict(state_dict)
    return model


@register_model
def mambaout_base_rw(pretrained=False, **kwargs):
    model = MambaOut(
        depths=(3, 4, 27, 3),
        dims=(128, 256, 512, 768),
        ls_init_value=1e-6,
        **kwargs
    )
    model.default_cfg = default_cfgs['mambaout_base']
    if pretrained:
        state_dict = torch.hub.load_state_dict_from_url(
            url=model.default_cfg['url'], map_location="cpu", check_hash=True)
        model.load_state_dict(state_dict)
    return model
