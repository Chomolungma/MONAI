# Copyright 2020 MONAI Consortium
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch
import torch.nn.functional as F
from monai.data.utils import dense_patch_slices, compute_importance_map


def sliding_window_inference(inputs, roi_size, sw_batch_size, predictor, overlap=0.25, blend_mode="constant"):
    """Use SlidingWindow method to execute inference.

    Args:
        inputs (torch Tensor): input image to be processed (assuming NCHW[D])
        roi_size (list, tuple): the window size to execute SlidingWindow inference.
        sw_batch_size (int): the batch size to run window slices.
        predictor (Callable): given input tensor `patch_data` in shape NCHW[D], `predictor(patch_data)`
            should return a prediction with the same spatial shape and batch_size, i.e. NMHW[D];
            where HW[D] represents the patch spatial size, M is the number of output channels, N is `sw_batch_size`.
        overlap (float): Amount of overlap between scans.
        blend_mode (str): How to blend output of overlapping windows. Options are 'constant', 'guassian'. 'constant'
            gives equal weight to all predictions while gaussian gives less weight to predictions on edges of windows.

    Note:
        must be channel first, support both 2D and 3D.
        input data must have batch dim.
        execute on 1 image/per inference, run a batch of window slices of 1 input image.
    """
    num_spatial_dims = len(inputs.shape) - 2
    assert len(roi_size) == num_spatial_dims, f"roi_size {roi_size} does not match input dims."
    assert overlap >= 0 and overlap < 1, "overlap must be >= 0 and < 1."

    # determine image spatial size and batch size
    # Note: all input images must have the same image size and batch size
    image_size = list(inputs.shape[2:])
    batch_size = inputs.shape[0]

    # TODO: Enable batch sizes > 1 in future
    if batch_size > 1:
        raise NotImplementedError

    original_image_size = [image_size[i] for i in range(num_spatial_dims)]
    # in case that image size is smaller than roi size
    image_size = tuple(max(image_size[i], roi_size[i]) for i in range(num_spatial_dims))
    pad_size = [i for k in range(len(inputs.shape) - 1, 1, -1) for i in (0, max(roi_size[k - 2] - inputs.shape[k], 0))]
    inputs = F.pad(inputs, pad=pad_size, mode="constant", value=0)

    scan_interval = _get_scan_interval(image_size, roi_size, num_spatial_dims, overlap)

    # Store all slices in list
    slices = dense_patch_slices(image_size, roi_size, scan_interval)

    slice_batches = []
    for slice_index in range(0, len(slices), sw_batch_size):
        slice_index_range = range(slice_index, min(slice_index + sw_batch_size, len(slices)))
        input_slices = []
        for curr_index in slice_index_range:
            if num_spatial_dims == 3:
                slice_i, slice_j, slice_k = slices[curr_index]
                input_slices.append(inputs[0, :, slice_i, slice_j, slice_k])
            else:
                slice_i, slice_j = slices[curr_index]
                input_slices.append(inputs[0, :, slice_i, slice_j])
        slice_batches.append(torch.stack(input_slices))

    # Perform predictions
    output_rois = list()
    for data in slice_batches:
        seg_prob = predictor(data)  # batched patch segmentation
        output_rois.append(seg_prob)

    # stitching output image
    output_classes = output_rois[0].shape[1]
    output_shape = [batch_size, output_classes] + list(image_size)

    # Create importance map
    importance_map = compute_importance_map(roi_size, mode=blend_mode, device=inputs.device)

    # allocate memory to store the full output and the count for overlapping parts
    output_image = torch.zeros(output_shape, dtype=torch.float32, device=inputs.device)
    count_map = torch.zeros(output_shape, dtype=torch.float32, device=inputs.device)

    for window_id, slice_index in enumerate(range(0, len(slices), sw_batch_size)):
        slice_index_range = range(slice_index, min(slice_index + sw_batch_size, len(slices)))

        # store the result in the proper location of the full output. Apply weights from importance map.
        for curr_index in slice_index_range:
            if num_spatial_dims == 3:
                slice_i, slice_j, slice_k = slices[curr_index]
                output_image[0, :, slice_i, slice_j, slice_k] += (
                    importance_map * output_rois[window_id][curr_index - slice_index, :]
                )
                count_map[0, :, slice_i, slice_j, slice_k] += importance_map
            else:
                slice_i, slice_j = slices[curr_index]
                output_image[0, :, slice_i, slice_j] += (
                    importance_map * output_rois[window_id][curr_index - slice_index, :]
                )
                count_map[0, :, slice_i, slice_j] += importance_map

    # account for any overlapping sections
    output_image /= count_map

    if num_spatial_dims == 3:
        return output_image[..., : original_image_size[0], : original_image_size[1], : original_image_size[2]]
    return output_image[..., : original_image_size[0], : original_image_size[1]]  # 2D


def _get_scan_interval(image_size, roi_size, num_spatial_dims, overlap):
    assert len(image_size) == num_spatial_dims, "image coord different from spatial dims."
    assert len(roi_size) == num_spatial_dims, "roi coord different from spatial dims."

    scan_interval = []
    for i in range(num_spatial_dims):
        if roi_size[i] == image_size[i]:
            scan_interval.append(int(roi_size[i]))
        else:
            # scan interval is (1-overlap)*roi_size
            scan_interval.append(int(roi_size[i] * (1 - overlap)))
    return tuple(scan_interval)
