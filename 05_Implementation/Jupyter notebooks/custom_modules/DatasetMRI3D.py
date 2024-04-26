from DatasetMRI import DatasetMRI
import nibabel as nib
import numpy as np
from pathlib import Path
from typing import Tuple
import torch
import os
from scipy.ndimage import label
from tqdm.auto import tqdm

class DatasetMRI3D(DatasetMRI):
    def __init__(
            self, 
            root_dir_img: Path, 
            root_dir_segm: Path = None, 
            root_dir_masks: Path = None, 
            root_dir_synthesis: Path = None, 
            img_target_shape = (256,256),  
            only_connected_masks: bool = False,
            min_volume=400):
        super().__init__(
            root_dir_img, 
            root_dir_segm, 
            root_dir_masks, 
            root_dir_synthesis, 
            img_target_shape,  
            only_connected_masks)
        self.min_volume=min_volume

        idx_dict=0 
        for idx_t1n in tqdm(np.arange(len(self.list_paths_t1n))): 
            idx_mask=0
            # go through every mask of the current t1n image and add  slices to dict
            while True:  
                path_component_matrix = None
                list_component_labels = None
                if only_connected_masks:
                    path_component_matrix, list_component_labels = self._get_relevant_components(self.list_paths_masks[idx_t1n][idx_mask], self.list_paths_t1n[idx_t1n], self.list_paths_segm[idx_t1n])
                    # if there are no connected components which reach the minimum area, skip this mask
                    if len(list_component_labels) == 0:
                        idx_mask+=1
                        continue
 
                self.idx_to_element[idx_dict]=(
                    self.list_paths_t1n[idx_t1n], 
                    self.list_paths_segm[idx_t1n] if self.list_paths_segm else None, 
                    self.list_paths_masks[idx_t1n][idx_mask] if self.list_paths_masks else None,
                    path_component_matrix,
                    list_component_labels,
                    self.list_paths_synthesis[idx_t1n] if self.list_paths_synthesis else None,) 
                idx_dict += 1

                if(self.list_paths_masks and len(self.list_paths_masks[idx_t1n])-1>idx_mask):
                    idx_mask+=1 
                else:
                    break

    def __getitem__(self, idx): 
            t1n_path = self.idx_to_element[idx][0] 
            segm_path = self.idx_to_element[idx][1]
            mask_path = self.idx_to_element[idx][2]
            if self.only_connected_masks:
                component_matrix_path = self.idx_to_element[idx][3]
                components = self.idx_to_element[idx][4]
            synthesis_path = self.idx_to_element[idx][5] 

            # load t1n img
            t1n_img = nib.load(t1n_path) 
            mask = nib.load(mask_path) if mask_path else None 
            t1n_segm = nib.load(segm_path) if segm_path else None 
            synthesis_mask = nib.load(synthesis_path) if synthesis_path else None

            # preprocess t1n, mask, segm and synthesis
            t1n_img, proc_info, mask, t1n_segm, synthesis_mask = self.preprocess(t1n_img, mask, t1n_segm, synthesis_mask)

            t1n_segm = t1n_segm if segm_path else torch.empty(0)
            synthesis_mask = synthesis_mask if synthesis_path else torch.empty(0)

            # load masks 
            if(mask_path): 
                if self.only_connected_masks: 
                    # create mask from random connected components
                    component_matrix = torch.load(component_matrix_path) 
                    rand_n = 1 if len(components) == 1 else torch.randint(1, len(components), (1,)).item() 
                    rand_components_idx = torch.randperm(len(components))[:rand_n] 
                    mask = torch.zeros_like(component_matrix)
                    for rand_idx in rand_components_idx: 
                        mask[component_matrix == components[rand_idx]] = 1
                else: 
                    # if there is a segmentation restrict mask to white matter regions
                    if(segm_path):
                        binary_white_matter_segm = self._get_white_matter_segm(t1n_segm) 
                        mask = binary_white_matter_segm * mask  
            else:
                mask = torch.empty(0) 
            
            # Output data
            sample_dict = {
                "gt_image": t1n_img.unsqueeze(0), 
                "segm": t1n_segm, 
                "mask": mask.unsqueeze(0),
                "synthesis": synthesis_mask.unsqueeze(0),
                "idx": int(idx), 
                "name": t1n_path.parent.stem,
                "proc_info": [proc_info]
            } 
            return sample_dict
    
    def _get_relevant_components(self, path_mask, path_t1n, path_segm):
        #get mask and calculcate components
        t1n_mask = self._get_mask(path_mask, path_t1n, path_segm)
        if (t1n_mask is None):
            return None, []

        path_component_matrix = os.path.splitext(path_mask)[0] + "_component_matrix.npy"
        component_matrix, n = self._get_component_matrix(t1n_mask, path_component_matrix) 

        # get only connected masks with a minimum volume 
        list_component_labels=[]
        for j in torch.arange(1, n+1):
            volume=torch.count_nonzero(component_matrix==j)
            if volume > self.min_volume:
                list_component_labels.append(int(j)) 

        return path_component_matrix, list_component_labels