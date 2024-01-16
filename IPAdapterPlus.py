import torch
import contextlib
import os
import math

import comfy.utils
import comfy.model_management
from comfy.clip_vision import clip_preprocess
from comfy.ldm.modules.attention import optimized_attention
import folder_paths

from torch import nn
from PIL import Image
import torch.nn.functional as F
import torchvision.transforms as TT

from .resampler import Resampler

# set the models directory backward compatible
GLOBAL_MODELS_DIR = os.path.join(folder_paths.models_dir, "ipadapter")
MODELS_DIR = GLOBAL_MODELS_DIR if os.path.isdir(GLOBAL_MODELS_DIR) else os.path.join(os.path.dirname(os.path.realpath(__file__)), "models")
if "ipadapter" not in folder_paths.folder_names_and_paths:
    current_paths = [MODELS_DIR]
else:
    current_paths, _ = folder_paths.folder_names_and_paths["ipadapter"]
folder_paths.folder_names_and_paths["ipadapter"] = (current_paths, folder_paths.supported_pt_extensions)

INSIGHTFACE_DIR = os.path.join(folder_paths.models_dir, "insightface")

class MLPProjModel(torch.nn.Module):
    """SD model with image prompt"""
    def __init__(self, cross_attention_dim=1024, clip_embeddings_dim=1024):
        super().__init__()
        
        self.proj = torch.nn.Sequential(
            torch.nn.Linear(clip_embeddings_dim, clip_embeddings_dim),
            torch.nn.GELU(),
            torch.nn.Linear(clip_embeddings_dim, cross_attention_dim),
            torch.nn.LayerNorm(cross_attention_dim)
        )
        
    def forward(self, image_embeds):
        clip_extra_context_tokens = self.proj(image_embeds)
        return clip_extra_context_tokens

class MLPProjModelFaceId(torch.nn.Module):
    """SD model with image prompt"""
    def __init__(self, cross_attention_dim=768, id_embeddings_dim=512, num_tokens=4):
        super().__init__()

        self.cross_attention_dim = cross_attention_dim
        self.num_tokens = num_tokens

        self.proj = torch.nn.Sequential(
            torch.nn.Linear(id_embeddings_dim, id_embeddings_dim*2),
            torch.nn.GELU(),
            torch.nn.Linear(id_embeddings_dim*2, cross_attention_dim*num_tokens),
        )
        self.norm = torch.nn.LayerNorm(cross_attention_dim)

    def forward(self, id_embeds):
        clip_extra_context_tokens = self.proj(id_embeds)
        clip_extra_context_tokens = clip_extra_context_tokens.reshape(-1, self.num_tokens, self.cross_attention_dim)
        clip_extra_context_tokens = self.norm(clip_extra_context_tokens)
        return clip_extra_context_tokens

class ImageProjModel(nn.Module):
    def __init__(self, cross_attention_dim=1024, clip_embeddings_dim=1024, clip_extra_context_tokens=4):
        super().__init__()
        
        self.cross_attention_dim = cross_attention_dim
        self.clip_extra_context_tokens = clip_extra_context_tokens
        self.proj = nn.Linear(clip_embeddings_dim, self.clip_extra_context_tokens * cross_attention_dim)
        self.norm = nn.LayerNorm(cross_attention_dim)
        
    def forward(self, image_embeds):
        embeds = image_embeds
        clip_extra_context_tokens = self.proj(embeds).reshape(-1, self.clip_extra_context_tokens, self.cross_attention_dim)
        clip_extra_context_tokens = self.norm(clip_extra_context_tokens)
        return clip_extra_context_tokens

class To_KV(nn.Module):
    def __init__(self, state_dict):
        super().__init__()

        self.to_kvs = nn.ModuleDict()
        for key, value in state_dict.items():
            self.to_kvs[key.replace(".weight", "").replace(".", "_")] = nn.Linear(value.shape[1], value.shape[0], bias=False)
            self.to_kvs[key.replace(".weight", "").replace(".", "_")].weight.data = value

def set_model_patch_replace(model, patch_kwargs, key):
    to = model.model_options["transformer_options"]
    if "patches_replace" not in to:
        to["patches_replace"] = {}
    if "attn2" not in to["patches_replace"]:
        to["patches_replace"]["attn2"] = {}
    if key not in to["patches_replace"]["attn2"]:
        patch = CrossAttentionPatch(**patch_kwargs)
        to["patches_replace"]["attn2"][key] = patch
    else:
        to["patches_replace"]["attn2"][key].set_new_condition(**patch_kwargs)

def image_add_noise(image, noise):
    image = image.permute([0,3,1,2])
    torch.manual_seed(8) # use a fixed random for reproducible results
    transforms = TT.Compose([
        TT.CenterCrop(min(image.shape[2], image.shape[3])),
        TT.Resize((224, 224), interpolation=TT.InterpolationMode.BICUBIC, antialias=True),
        TT.ElasticTransform(alpha=75.0, sigma=noise*3.5), # shuffle the image
        TT.RandomVerticalFlip(p=1.0), # flip the image to change the geometry even more
        TT.RandomHorizontalFlip(p=1.0),
    ])
    image = transforms(image.cpu())
    image = image.permute([0,2,3,1])
    image = image + ((0.25*(1-noise)+0.05) * torch.randn_like(image) )   # add further random noise
    return image

def zeroed_hidden_states(clip_vision, batch_size):
    image = torch.zeros([batch_size, 224, 224, 3])
    comfy.model_management.load_model_gpu(clip_vision.patcher)
    pixel_values = clip_preprocess(image.to(clip_vision.load_device)).float()
    outputs = clip_vision.model(pixel_values=pixel_values, intermediate_output=-2)
    # we only need the penultimate hidden states
    outputs = outputs[1].to(comfy.model_management.intermediate_device())
    return outputs

def min_(tensor_list):
    # return the element-wise min of the tensor list.
    x = torch.stack(tensor_list)
    mn = x.min(axis=0)[0]
    return torch.clamp(mn, min=0)
    
def max_(tensor_list):
    # return the element-wise max of the tensor list.
    x = torch.stack(tensor_list)
    mx = x.max(axis=0)[0]
    return torch.clamp(mx, max=1)

# From https://github.com/Jamy-L/Pytorch-Contrast-Adaptive-Sharpening/
def contrast_adaptive_sharpening(image, amount):
    img = F.pad(image, pad=(1, 1, 1, 1)).cpu()

    a = img[..., :-2, :-2]
    b = img[..., :-2, 1:-1]
    c = img[..., :-2, 2:]
    d = img[..., 1:-1, :-2]
    e = img[..., 1:-1, 1:-1]
    f = img[..., 1:-1, 2:]
    g = img[..., 2:, :-2]
    h = img[..., 2:, 1:-1]
    i = img[..., 2:, 2:]
    
    # Computing contrast
    cross = (b, d, e, f, h)
    mn = min_(cross)
    mx = max_(cross)
    
    diag = (a, c, g, i)
    mn2 = min_(diag)
    mx2 = max_(diag)
    mx = mx + mx2
    mn = mn + mn2
    
    # Computing local weight
    inv_mx = torch.reciprocal(mx)
    amp = inv_mx * torch.minimum(mn, (2 - mx))

    # scaling
    amp = torch.sqrt(amp)
    w = - amp * (amount * (1/5 - 1/8) + 1/8)
    div = torch.reciprocal(1 + 4*w)

    output = ((b + d + f + h)*w + e) * div
    output = output.clamp(0, 1)
    output = torch.nan_to_num(output)

    return (output)

def tensorToCV(image):
    import numpy as np

    # TODO: there must be a better way
    out = []
    image = image.detach().cpu()
    for i in range(image.shape[0]):
        img = image[i]
        img = img[..., [2, 1, 0]] # Convert from RGB to BGR
        img = torch.clamp(img * 255, 0, 255).to(torch.uint8) # Scale pixel values up to [0, 255] if they are in [0, 1]
        img = img.numpy()
        out.append(img)

    out = np.stack(img, axis=0)
    return out

class IPAdapter():
    def __init__(self, ipadapter_model, cross_attention_dim=1024, output_cross_attention_dim=1024, clip_embeddings_dim=1024, clip_extra_context_tokens=4, is_sdxl=False, is_plus=False, is_full=False, is_faceid=False):
        super().__init__()

        self.clip_embeddings_dim = clip_embeddings_dim
        self.cross_attention_dim = cross_attention_dim
        self.output_cross_attention_dim = output_cross_attention_dim
        self.clip_extra_context_tokens = clip_extra_context_tokens
        self.is_sdxl = is_sdxl
        self.is_full = is_full

        if is_faceid:
            self.image_proj_model = self.init_proj_faceid()
        elif is_plus:
            self.image_proj_model = self.init_proj_plus()
        else:
            self.image_proj_model = self.init_proj()

        self.image_proj_model.load_state_dict(ipadapter_model["image_proj"])

    def init_proj(self):
        image_proj_model = ImageProjModel(
            cross_attention_dim=self.cross_attention_dim,
            clip_embeddings_dim=self.clip_embeddings_dim,
            clip_extra_context_tokens=self.clip_extra_context_tokens
        )
        return image_proj_model

    def init_proj_plus(self):
        if self.is_full:
            image_proj_model = MLPProjModel(
                cross_attention_dim=self.cross_attention_dim,
                clip_embeddings_dim=self.clip_embeddings_dim
            )
        else:
            image_proj_model = Resampler(
                dim=self.cross_attention_dim,
                depth=4,
                dim_head=64,
                heads=20 if self.is_sdxl else 12,
                num_queries=self.clip_extra_context_tokens,
                embedding_dim=self.clip_embeddings_dim,
                output_dim=self.output_cross_attention_dim,
                ff_mult=4
            )
        return image_proj_model

    def init_proj_faceid(self):
        image_proj_model = MLPProjModelFaceId(
            cross_attention_dim=self.cross_attention_dim,
            id_embeddings_dim=512,
            num_tokens=self.clip_extra_context_tokens,
        )
        return image_proj_model

    @torch.inference_mode()
    def get_image_embeds(self, clip_embed, clip_embed_zeroed, device, dtype):
        self.image_proj_model.to(device, dtype=dtype)
        image_prompt_embeds = self.image_proj_model(clip_embed.to(device, dtype=dtype))
        uncond_image_prompt_embeds = self.image_proj_model(clip_embed_zeroed.to(device, dtype=dtype))
        del self.image_proj_model  # free GPU memory
        return image_prompt_embeds, uncond_image_prompt_embeds

class CrossAttentionPatch:
    # forward for patching
    def __init__(self, weight, ip_layers, number, cond, uncond, weight_type, mask=None, sigma_start=0.0, sigma_end=1.0, unfold_batch=False):
        self.weights = [weight]
        self.ip_layers = [ip_layers]
        self.conds = [cond]
        self.unconds = [uncond]
        self.number = number
        self.weight_type = [weight_type]
        self.masks = [mask]
        self.sigma_start = [sigma_start]
        self.sigma_end = [sigma_end]
        self.unfold_batch = [unfold_batch]

        self.k_key = str(self.number*2+1) + "_to_k_ip"
        self.v_key = str(self.number*2+1) + "_to_v_ip"
        self.current_device = cond.device
    
    def set_new_condition(self, weight, ip_layers, number, cond, uncond, weight_type, mask=None, sigma_start=0.0, sigma_end=1.0, unfold_batch=False):
        self.weights.append(weight)
        self.ip_layers.append(ip_layers)
        self.conds.append(cond)
        self.unconds.append(uncond)
        self.masks.append(mask)
        self.weight_type.append(weight_type)
        self.sigma_start.append(sigma_start)
        self.sigma_end.append(sigma_end)
        self.unfold_batch.append(unfold_batch)

    def to(self, device_or_dtype):
        ''' move to device or convert to dtype '''
        if not isinstance(device_or_dtype, torch.device): # ignore dtype conversions
            return self
        dtype = torch.float16 if comfy.model_management.should_use_fp16() else torch.float32
        for i in range(len(self.conds)):
            self.conds[i] = self.conds[i].to(device_or_dtype, dtype=dtype)
            self.unconds[i] = self.unconds[i].to(device_or_dtype, dtype=dtype)
            if self.masks[i] is not None:
              self.masks[i] = self.masks[i].to(device_or_dtype, dtype=dtype)
            self.ip_layers[i] = self.ip_layers[i].to(device_or_dtype, dtype=dtype)
        return self

    def __call__(self, n, context_attn2, value_attn2, extra_options):
        org_dtype = n.dtype
        cond_or_uncond = extra_options["cond_or_uncond"]
        sigma = extra_options["sigmas"][0].item() if 'sigmas' in extra_options else 999999999.9

        # extra options for AnimateDiff
        ad_params = extra_options['ad_params'] if "ad_params" in extra_options else None

        q = n
        k = context_attn2
        v = value_attn2
        b = q.shape[0]
        qs = q.shape[1]
        batch_prompt = b // len(cond_or_uncond)
        out = optimized_attention(q, k, v, extra_options["n_heads"])
        _, _, lh, lw = extra_options["original_shape"]
        
        for weight, cond, uncond, ip_layers, mask, weight_type, sigma_start, sigma_end, unfold_batch in zip(self.weights, self.conds, self.unconds, self.ip_layers, self.masks, self.weight_type, self.sigma_start, self.sigma_end, self.unfold_batch):
            if sigma > sigma_start or sigma < sigma_end:
                continue
            if unfold_batch and cond.shape[0] > 1:
                # Check AnimateDiff context window
                if ad_params is not None and ad_params["sub_idxs"] is not None:
                    # if images length matches or exceeds full_length get sub_idx images
                    if cond.shape[0] >= ad_params["full_length"]:
                        cond = torch.Tensor(cond[ad_params["sub_idxs"]])
                        uncond = torch.Tensor(uncond[ad_params["sub_idxs"]])
                    # otherwise, need to do more to get proper sub_idxs masks
                    else:
                        # check if images length matches full_length - if not, make it match
                        if cond.shape[0] < ad_params["full_length"]:
                            cond = torch.cat((cond, cond[-1:].repeat((ad_params["full_length"]-cond.shape[0], 1, 1))), dim=0)
                            uncond = torch.cat((uncond, uncond[-1:].repeat((ad_params["full_length"]-uncond.shape[0], 1, 1))), dim=0)
                        # if we have too many remove the excess (should not happen, but just in case)
                        if cond.shape[0] > ad_params["full_length"]:
                            cond = cond[:ad_params["full_length"]]
                            uncond = uncond[:ad_params["full_length"]]
                        cond = cond[ad_params["sub_idxs"]]
                        uncond = uncond[ad_params["sub_idxs"]]

                # if we don't have enough reference images repeat the last one until we reach the right size
                if cond.shape[0] < batch_prompt:
                    cond = torch.cat((cond, cond[-1:].repeat((batch_prompt-cond.shape[0], 1, 1))), dim=0)
                    uncond = torch.cat((uncond, uncond[-1:].repeat((batch_prompt-uncond.shape[0], 1, 1))), dim=0)
                # if we have too many remove the exceeding
                elif cond.shape[0] > batch_prompt:
                    cond = cond[:batch_prompt]
                    uncond = uncond[:batch_prompt]

                k_cond = ip_layers.to_kvs[self.k_key](cond)
                k_uncond = ip_layers.to_kvs[self.k_key](uncond)
                v_cond = ip_layers.to_kvs[self.v_key](cond)
                v_uncond = ip_layers.to_kvs[self.v_key](uncond)
            else:
                k_cond = ip_layers.to_kvs[self.k_key](cond).repeat(batch_prompt, 1, 1)
                k_uncond = ip_layers.to_kvs[self.k_key](uncond).repeat(batch_prompt, 1, 1)
                v_cond = ip_layers.to_kvs[self.v_key](cond).repeat(batch_prompt, 1, 1)
                v_uncond = ip_layers.to_kvs[self.v_key](uncond).repeat(batch_prompt, 1, 1)

            if weight_type.startswith("linear"):
                ip_k = torch.cat([(k_cond, k_uncond)[i] for i in cond_or_uncond], dim=0) * weight
                ip_v = torch.cat([(v_cond, v_uncond)[i] for i in cond_or_uncond], dim=0) * weight
            else:
                ip_k = torch.cat([(k_cond, k_uncond)[i] for i in cond_or_uncond], dim=0)
                ip_v = torch.cat([(v_cond, v_uncond)[i] for i in cond_or_uncond], dim=0)

                if weight_type.startswith("channel"):
                    # code by Lvmin Zhang at Stanford University as also seen on Fooocus IPAdapter implementation
                    # please read licensing notes https://github.com/lllyasviel/Fooocus/blob/69a23c4d60c9e627409d0cb0f8862cdb015488eb/extras/ip_adapter.py#L234
                    ip_v_mean = torch.mean(ip_v, dim=1, keepdim=True)
                    ip_v_offset = ip_v - ip_v_mean
                    _, _, C = ip_k.shape
                    channel_penalty = float(C) / 1280.0
                    W = weight * channel_penalty
                    ip_k = ip_k * W
                    ip_v = ip_v_offset + ip_v_mean * W

            out_ip = optimized_attention(q, ip_k, ip_v, extra_options["n_heads"])           
            if weight_type.startswith("original"):
                out_ip = out_ip * weight

            if mask is not None:
                # TODO: needs checking
                mask_h = lh / math.sqrt(lh * lw / qs)
                mask_h = int(mask_h) + int((qs % int(mask_h)) != 0)
                mask_w = qs // mask_h

                # check if using AnimateDiff and sliding context window
                if (mask.shape[0] > 1 and ad_params is not None and ad_params["sub_idxs"] is not None):
                    # if mask length matches or exceeds full_length, just get sub_idx masks, resize, and continue
                    if mask.shape[0] >= ad_params["full_length"]:
                        mask_downsample = torch.Tensor(mask[ad_params["sub_idxs"]])
                        mask_downsample = F.interpolate(mask_downsample.unsqueeze(1), size=(mask_h, mask_w), mode="bicubic").squeeze(1)
                    # otherwise, need to do more to get proper sub_idxs masks
                    else:
                        # resize to needed attention size (to save on memory)
                        mask_downsample = F.interpolate(mask.unsqueeze(1), size=(mask_h, mask_w), mode="bicubic").squeeze(1)
                        # check if mask length matches full_length - if not, make it match
                        if mask_downsample.shape[0] < ad_params["full_length"]:
                            mask_downsample = torch.cat((mask_downsample, mask_downsample[-1:].repeat((ad_params["full_length"]-mask_downsample.shape[0], 1, 1))), dim=0)
                        # if we have too many remove the excess (should not happen, but just in case)
                        if mask_downsample.shape[0] > ad_params["full_length"]:
                            mask_downsample = mask_downsample[:ad_params["full_length"]]
                        # now, select sub_idxs masks
                        mask_downsample = mask_downsample[ad_params["sub_idxs"]]
                # otherwise, perform usual mask interpolation
                else:
                    mask_downsample = F.interpolate(mask.unsqueeze(1), size=(mask_h, mask_w), mode="bicubic").squeeze(1)

                # if we don't have enough masks repeat the last one until we reach the right size
                if mask_downsample.shape[0] < batch_prompt:
                    mask_downsample = torch.cat((mask_downsample, mask_downsample[-1:, :, :].repeat((batch_prompt-mask_downsample.shape[0], 1, 1))), dim=0)
                # if we have too many remove the exceeding
                elif mask_downsample.shape[0] > batch_prompt:
                    mask_downsample = mask_downsample[:batch_prompt, :, :]
                
                # repeat the masks
                mask_downsample = mask_downsample.repeat(len(cond_or_uncond), 1, 1)
                mask_downsample = mask_downsample.view(mask_downsample.shape[0], -1, 1).repeat(1, 1, out.shape[2])

                out_ip = out_ip * mask_downsample

            out = out + out_ip

        return out.to(dtype=org_dtype)

class IPAdapterModelLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": { "ipadapter_file": (folder_paths.get_filename_list("ipadapter"), )}}

    RETURN_TYPES = ("IPADAPTER",)
    FUNCTION = "load_ipadapter_model"

    CATEGORY = "ipadapter"

    def load_ipadapter_model(self, ipadapter_file):
        ckpt_path = folder_paths.get_full_path("ipadapter", ipadapter_file)

        model = comfy.utils.load_torch_file(ckpt_path, safe_load=True)

        if ckpt_path.lower().endswith(".safetensors"):
            st_model = {"image_proj": {}, "ip_adapter": {}}
            for key in model.keys():
                if key.startswith("image_proj."):
                    st_model["image_proj"][key.replace("image_proj.", "")] = model[key]
                elif key.startswith("ip_adapter."):
                    st_model["ip_adapter"][key.replace("ip_adapter.", "")] = model[key]
            model = st_model
                    
        if not "ip_adapter" in model.keys() or not model["ip_adapter"]:
            raise Exception("invalid IPAdapter model {}".format(ckpt_path))

        return (model,)

class InsightFaceLoader:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "provider": (["CPU", "CUDA", "ROCM"], ),
            },
        }

    RETURN_TYPES = ("CLIP_VISION",)
    FUNCTION = "load_insight_face"

    CATEGORY = "ipadapter"

    def load_insight_face(self, provider):
        try:
            from insightface.app import FaceAnalysis
        except ImportError:
            raise Exception('IPAdapter: InsightFace is not installed! Install the missing dependencies if you wish to use FaceID models.')

        model = FaceAnalysis(name="buffalo_l", root=INSIGHTFACE_DIR, providers=[provider + 'ExecutionProvider',])
        model.prepare(ctx_id=0, det_size=(640, 640))

        return (model,)

class IPAdapterApply:
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "ipadapter": ("IPADAPTER", ),
                "clip_vision": ("CLIP_VISION",),
                "image": ("IMAGE",),
                "model": ("MODEL", ),
                "weight": ("FLOAT", { "default": 1.0, "min": -1, "max": 3, "step": 0.05 }),
                "noise": ("FLOAT", { "default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01 }),
                "weight_type": (["original", "linear", "channel penalty"], ),
                "start_at": ("FLOAT", { "default": 0.0, "min": 0.0, "max": 1.0, "step": 0.001 }),
                "end_at": ("FLOAT", { "default": 1.0, "min": 0.0, "max": 1.0, "step": 0.001 }),
                "unfold_batch": ("BOOLEAN", { "default": False }),
            },
            "optional": {
                "attn_mask": ("MASK",),
            }
        }

    RETURN_TYPES = ("MODEL",)
    FUNCTION = "apply_ipadapter"
    CATEGORY = "ipadapter"

    def apply_ipadapter(self, ipadapter, model, weight, clip_vision=None, image=None, weight_type="original", noise=None, embeds=None, attn_mask=None, start_at=0.0, end_at=1.0, unfold_batch=False):
        dtype = torch.float16 if comfy.model_management.should_use_fp16() else torch.float32
        device = comfy.model_management.get_torch_device()
        weight = weight
        is_full = "proj.0.weight" in ipadapter["image_proj"]
        is_faceid = "0.to_q_lora.down.weight" in ipadapter["ip_adapter"] # TODO: better way to detect faceid?
        is_plus = (is_full or "latents" in ipadapter["image_proj"]) and not is_faceid

        output_cross_attention_dim = ipadapter["ip_adapter"]["1.to_k_ip.weight"].shape[1]
        is_sdxl = output_cross_attention_dim == 2048
        cross_attention_dim = 1280 if is_plus and is_sdxl else output_cross_attention_dim
        clip_extra_context_tokens = 16 if is_plus else 4

        if embeds is not None:
            embeds = torch.unbind(embeds)
            clip_embed = embeds[0].cpu()
            clip_embed_zeroed = embeds[1].cpu()
        else:
            if is_faceid:
                clip_embed = clip_vision.get(tensorToCV(image)) # TODO: support multiple images (is it needed?)
                if not clip_embed:
                    print("\033[33mWARNING!!! InsightFace wasn't able to detect the face. Try to use the PrepImageForInsightFace node.\033[0m")

                #clip_embed = torch.from_numpy(clip_embed[0].normed_embedding).unsqueeze(0)
                clip_embed = torch.stack([torch.from_numpy(elem.normed_embedding).unsqueeze(0) for elem in clip_embed], dim=0)
                clip_embed_zeroed = torch.zeros_like(clip_embed)
            else:
                if image.shape[1] != image.shape[2]:
                    print("\033[33mINFO: the IPAdapter reference image is not a square, CLIPImageProcessor will resize and crop it at the center. If the main focus of the picture is not in the middle the result might not be what you are expecting.\033[0m")

                clip_embed = clip_vision.encode_image(image)
                neg_image = image_add_noise(image, noise) if noise > 0 else None
                
                if is_plus:
                    clip_embed = clip_embed.penultimate_hidden_states
                    if noise > 0:
                        clip_embed_zeroed = clip_vision.encode_image(neg_image).penultimate_hidden_states
                    else:
                        clip_embed_zeroed = zeroed_hidden_states(clip_vision, image.shape[0])
                else:
                    clip_embed = clip_embed.image_embeds
                    if noise > 0:
                        clip_embed_zeroed = clip_vision.encode_image(neg_image).image_embeds
                    else:
                        clip_embed_zeroed = torch.zeros_like(clip_embed)

        clip_embeddings_dim = clip_embed.shape[-1]

        ipadapter_inst = IPAdapter(
            ipadapter,
            cross_attention_dim=cross_attention_dim,
            output_cross_attention_dim=output_cross_attention_dim,
            clip_embeddings_dim=clip_embeddings_dim,
            clip_extra_context_tokens=clip_extra_context_tokens,
            is_sdxl=is_sdxl,
            is_plus=is_plus,
            is_full=is_full,
            is_faceid=is_faceid,
        )

        sigma_start = model.model.model_sampling.percent_to_sigma(start_at)
        sigma_end = model.model.model_sampling.percent_to_sigma(end_at)

        image_prompt_embeds, uncond_image_prompt_embeds = ipadapter_inst.get_image_embeds(clip_embed, clip_embed_zeroed, device, dtype)
        
        # offload to CPU memory
        image_prompt_embeds = image_prompt_embeds.to(model.offload_device, dtype=dtype)
        uncond_image_prompt_embeds = uncond_image_prompt_embeds.to(model.offload_device, dtype=dtype)

        del ipadapter_inst

        if attn_mask is not None:
            attn_mask = attn_mask.to(model.offload_device)

        ip_layers = To_KV(ipadapter["ip_adapter"])
        patch_kwargs = {
            "number": 0,
            "weight": weight,
            "ip_layers": ip_layers,
            "cond": image_prompt_embeds,
            "uncond": uncond_image_prompt_embeds,
            "weight_type": weight_type,
            "mask": attn_mask,
            "sigma_start": sigma_start,
            "sigma_end": sigma_end,
            "unfold_batch": unfold_batch,
        }

        work_model = model.clone()

        if not is_sdxl:
            for id in [1,2,4,5,7,8]: # id of input_blocks that have cross attention
                set_model_patch_replace(work_model, patch_kwargs, ("input", id))
                patch_kwargs["number"] += 1
            for id in [3,4,5,6,7,8,9,10,11]: # id of output_blocks that have cross attention
                set_model_patch_replace(work_model, patch_kwargs, ("output", id))
                patch_kwargs["number"] += 1
            set_model_patch_replace(work_model, patch_kwargs, ("middle", 0))
        else:
            for id in [4,5,7,8]: # id of input_blocks that have cross attention
                block_indices = range(2) if id in [4, 5] else range(10) # transformer_depth
                for index in block_indices:
                    set_model_patch_replace(work_model, patch_kwargs, ("input", id, index))
                    patch_kwargs["number"] += 1
            for id in range(6): # id of output_blocks that have cross attention
                block_indices = range(2) if id in [3, 4, 5] else range(10) # transformer_depth
                for index in block_indices:
                    set_model_patch_replace(work_model, patch_kwargs, ("output", id, index))
                    patch_kwargs["number"] += 1
            for index in range(10):
                set_model_patch_replace(work_model, patch_kwargs, ("middle", 0, index))
                patch_kwargs["number"] += 1

        return (work_model, )

def prepImage(image, interpolation="LANCZOS", crop_position="center", size=(224,224), sharpening=0.0, padding=0):
    _, oh, ow, _ = image.shape
    output = image.permute([0,3,1,2])

    if "pad" in crop_position:
        target_length = max(oh, ow)
        pad_l = (target_length - ow) // 2
        pad_r = (target_length - ow) - pad_l
        pad_t = (target_length - oh) // 2
        pad_b = (target_length - oh) - pad_t
        output = F.pad(output, (pad_l, pad_r, pad_t, pad_b), value=0, mode="constant")
    else:
        crop_size = min(oh, ow)
        x = (ow-crop_size) // 2
        y = (oh-crop_size) // 2
        if "top" in crop_position:
            y = 0
        elif "bottom" in crop_position:
            y = oh-crop_size
        elif "left" in crop_position:
            x = 0
        elif "right" in crop_position:
            x = ow-crop_size
        
        x2 = x+crop_size
        y2 = y+crop_size

        # crop
        output = output[:, :, y:y2, x:x2]

    # resize (apparently PIL resize is better than tourchvision interpolate)
    imgs = []
    for i in range(output.shape[0]):
        img = TT.ToPILImage()(output[i])
        img = img.resize(size, resample=Image.Resampling[interpolation])
        imgs.append(TT.ToTensor()(img))
    output = torch.stack(imgs, dim=0)
    imgs = None # zelous GC
    
    if sharpening > 0:
        output = contrast_adaptive_sharpening(output, sharpening)
    
    if padding > 0:
        output = F.pad(output, (padding, padding, padding, padding), value=255, mode="constant")

    output = output.permute([0,2,3,1])

    return output

class PrepImageForInsightFace:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "image": ("IMAGE",),
            "crop_position": (["center", "top", "bottom", "left", "right"],),
            "sharpening": ("FLOAT", {"default": 0.0, "min": 0, "max": 1, "step": 0.05}),
            "pad_around": ("BOOLEAN", { "default": True }),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "prep_image"

    CATEGORY = "ipadapter"

    def prep_image(self, image, crop_position, sharpening=0.0, pad_around=True):
        if pad_around:
            padding = 60
            size = (580, 580)
        else:
            padding = 0
            size = (640, 640)

        output = prepImage(image, "LANCZOS", crop_position, size, sharpening, padding)

        return (output, )

class PrepImageForClipVision:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "image": ("IMAGE",),
            "interpolation": (["LANCZOS", "BICUBIC", "HAMMING", "BILINEAR", "BOX", "NEAREST"],),
            "crop_position": (["top", "bottom", "left", "right", "center", "pad"],),
            "sharpening": ("FLOAT", {"default": 0.0, "min": 0, "max": 1, "step": 0.05}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "prep_image"

    CATEGORY = "ipadapter"

    def prep_image(self, image, interpolation="LANCZOS", crop_position="center", sharpening=0.0):
        size = (224, 224)
        output = prepImage(image, interpolation, crop_position, size, sharpening, 0)
        return (output, )

class IPAdapterEncoder:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "clip_vision": ("CLIP_VISION",),
            "image_1": ("IMAGE",),
            "ipadapter_plus": ("BOOLEAN", { "default": False }),
            "noise": ("FLOAT", { "default": 0.0, "min": 0.0, "max": 1.0, "step": 0.01 }),
            "weight_1": ("FLOAT", { "default": 1.0, "min": 0, "max": 1.0, "step": 0.01 }),
            },
            "optional": {
                "image_2": ("IMAGE",),
                "image_3": ("IMAGE",),
                "image_4": ("IMAGE",),
                "weight_2": ("FLOAT", { "default": 1.0, "min": 0, "max": 1.0, "step": 0.01 }),
                "weight_3": ("FLOAT", { "default": 1.0, "min": 0, "max": 1.0, "step": 0.01 }),
                "weight_4": ("FLOAT", { "default": 1.0, "min": 0, "max": 1.0, "step": 0.01 }),
            }
        }

    RETURN_TYPES = ("EMBEDS",)
    FUNCTION = "preprocess"
    CATEGORY = "ipadapter"

    def preprocess(self, clip_vision, image_1, ipadapter_plus, noise, weight_1, image_2=None, image_3=None, image_4=None, weight_2=1.0, weight_3=1.0, weight_4=1.0):
        weight_1 *= (0.1 + (weight_1 - 0.1))
        weight_1 = 1.19e-05 if weight_1 <= 1.19e-05 else weight_1
        weight_2 *= (0.1 + (weight_2 - 0.1))
        weight_2 = 1.19e-05 if weight_2 <= 1.19e-05 else weight_2
        weight_3 *= (0.1 + (weight_3 - 0.1))
        weight_3 = 1.19e-05 if weight_3 <= 1.19e-05 else weight_3
        weight_4 *= (0.1 + (weight_4 - 0.1))
        weight_5 = 1.19e-05 if weight_4 <= 1.19e-05 else weight_4

        image = image_1
        weight = [weight_1]*image_1.shape[0]
        
        if image_2 is not None:
            if image_1.shape[1:] != image_2.shape[1:]:
                image_2 = comfy.utils.common_upscale(image_2.movedim(-1,1), image.shape[2], image.shape[1], "bilinear", "center").movedim(1,-1)
            image = torch.cat((image, image_2), dim=0)
            weight += [weight_2]*image_2.shape[0]
        if image_3 is not None:
            if image.shape[1:] != image_3.shape[1:]:
                image_3 = comfy.utils.common_upscale(image_3.movedim(-1,1), image.shape[2], image.shape[1], "bilinear", "center").movedim(1,-1)
            image = torch.cat((image, image_3), dim=0)
            weight += [weight_3]*image_3.shape[0]
        if image_4 is not None:
            if image.shape[1:] != image_4.shape[1:]:
                image_4 = comfy.utils.common_upscale(image_4.movedim(-1,1), image.shape[2], image.shape[1], "bilinear", "center").movedim(1,-1)
            image = torch.cat((image, image_4), dim=0)
            weight += [weight_4]*image_4.shape[0]
        
        clip_embed = clip_vision.encode_image(image)
        neg_image = image_add_noise(image, noise) if noise > 0 else None
        
        if ipadapter_plus:
            clip_embed = clip_embed.penultimate_hidden_states
            if noise > 0:
                clip_embed_zeroed = clip_vision.encode_image(neg_image).penultimate_hidden_states
            else:
                clip_embed_zeroed = zeroed_hidden_states(clip_vision, image.shape[0])
        else:
            clip_embed = clip_embed.image_embeds
            if noise > 0:
                clip_embed_zeroed = clip_vision.encode_image(neg_image).image_embeds
            else:
                clip_embed_zeroed = torch.zeros_like(clip_embed)

        if any(e != 1.0 for e in weight):
            weight = torch.tensor(weight).unsqueeze(-1) if not ipadapter_plus else torch.tensor(weight).unsqueeze(-1).unsqueeze(-1)
            clip_embed = clip_embed * weight
        
        output = torch.stack((clip_embed, clip_embed_zeroed))

        return( output, )

class IPAdapterApplyEncoded(IPAdapterApply):
    @classmethod
    def INPUT_TYPES(s):
        return {
            "required": {
                "ipadapter": ("IPADAPTER", ),
                "embeds": ("EMBEDS",),
                "model": ("MODEL", ),
                "weight": ("FLOAT", { "default": 1.0, "min": -1, "max": 3, "step": 0.05 }),
                "weight_type": (["original", "linear", "channel penalty"], ),
                "start_at": ("FLOAT", { "default": 0.0, "min": 0.0, "max": 1.0, "step": 0.001 }),
                "end_at": ("FLOAT", { "default": 1.0, "min": 0.0, "max": 1.0, "step": 0.001 }),
                "unfold_batch": ("BOOLEAN", { "default": False }),
            },
            "optional": {
                "attn_mask": ("MASK",),
            }
        }

class IPAdapterSaveEmbeds:
    def __init__(self):
        self.output_dir = folder_paths.get_output_directory()

    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "embeds": ("EMBEDS",),
            "filename_prefix": ("STRING", {"default": "embeds/IPAdapter"})
            },
        }

    RETURN_TYPES = ()
    FUNCTION = "save"
    OUTPUT_NODE = True
    CATEGORY = "ipadapter"

    def save(self, embeds, filename_prefix):
        full_output_folder, filename, counter, subfolder, filename_prefix = folder_paths.get_save_image_path(filename_prefix, self.output_dir)
        file = f"{filename}_{counter:05}_.ipadpt"
        file = os.path.join(full_output_folder, file)

        torch.save(embeds, file)
        return (None, )


class IPAdapterLoadEmbeds:
    @classmethod
    def INPUT_TYPES(s):
        input_dir = folder_paths.get_input_directory()
        files = [os.path.relpath(os.path.join(root, file), input_dir) for root, dirs, files in os.walk(input_dir) for file in files if file.endswith('.ipadpt')]
        return {"required": {"embeds": [sorted(files), ]}, }

    RETURN_TYPES = ("EMBEDS", )
    FUNCTION = "load"
    CATEGORY = "ipadapter"

    def load(self, embeds):
        path = folder_paths.get_annotated_filepath(embeds)
        output = torch.load(path).cpu()

        return (output, )


class IPAdapterBatchEmbeds:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "embed1": ("EMBEDS",),
            "embed2": ("EMBEDS",),
        }}

    RETURN_TYPES = ("EMBEDS",)
    FUNCTION = "batch"
    CATEGORY = "ipadapter"

    def batch(self, embed1, embed2):
        output = torch.cat((embed1, embed2), dim=1)
        return (output, )

NODE_CLASS_MAPPINGS = {
    "IPAdapterModelLoader": IPAdapterModelLoader,
    "IPAdapterApply": IPAdapterApply,
    "IPAdapterApplyEncoded": IPAdapterApplyEncoded,
    "PrepImageForClipVision": PrepImageForClipVision,
    "IPAdapterEncoder": IPAdapterEncoder,
    "IPAdapterSaveEmbeds": IPAdapterSaveEmbeds,
    "IPAdapterLoadEmbeds": IPAdapterLoadEmbeds,
    "IPAdapterBatchEmbeds": IPAdapterBatchEmbeds,
    "InsightFaceLoader": InsightFaceLoader,
    "PrepImageForInsightFace": PrepImageForInsightFace,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "IPAdapterModelLoader": "Load IPAdapter Model",
    "IPAdapterApply": "Apply IPAdapter",
    "IPAdapterApplyEncoded": "Apply IPAdapter from Encoded",
    "PrepImageForClipVision": "Prepare Image For Clip Vision",
    "IPAdapterEncoder": "Encode IPAdapter Image",
    "IPAdapterSaveEmbeds": "Save IPAdapter Embeds",
    "IPAdapterLoadEmbeds": "Load IPAdapter Embeds",
    "IPAdapterBatchEmbeds": "IPAdapter Batch Embeds",
    "InsightFaceLoader": "Load InsightFace",
    "PrepImageForInsightFace": "Prepare Image For InsightFace",
}
