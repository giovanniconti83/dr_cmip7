#!/bin/bash -ex

dwl_dr_dir="CMIP7_DReq_Software_v1.4"
commit="5a2ea3f"
gitrepo="git@github.com:CMIP-Data-Request/CMIP7_DReq_Software.git"

git clone "${gitrepo}" "${dwl_dr_dir}"
cd "${dwl_dr_dir}"
git checkout "${commit}"

echo "Download completed"
