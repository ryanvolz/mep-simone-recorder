# SPDX-FileCopyrightText: Copyright (c) 2026 Massachusetts Institute of Technology
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

FROM ghcr.io/ryanvolz/holoscan_recorder/mep:latest AS simone
LABEL org.opencontainers.image.description="Holoscan SIMONe recorder"

# Copy scripts specific to this image
COPY --chmod=777 src/simone_recorder.py /app/simone_recorder.py
COPY --chmod=777 config /config

# Set up environment variable defaults for this image
ENV RECORDER_CONFIG_PATH=/config
ENV RECORDER_OUTPUT_PATH=/data/ringbuffer
ENV RECORDER_RAM_RINGBUFFER_PATH=/ramdisk
ENV RECORDER_SCRIPT_PATH=/app/simone_recorder.py
ENV RECORDER_START_CONFIG=default
ENV RECORDER_TMP_RINGBUFFER_PATH=/data/tmp-ringbuffer

ENV HOME=/ramdisk
WORKDIR /ramdisk
ENTRYPOINT ["python3", "/app/recorder_service.py"]

############################################################
# Default target
############################################################
FROM simone AS default
