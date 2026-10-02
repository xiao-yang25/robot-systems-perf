ARG BASE_IMAGE=ros:humble-ros-base-jammy
FROM ${BASE_IMAGE}
ARG SOURCE_REVISION=uncommitted
ARG BASE_IMAGE_ID=unknown
ENV EP_SOURCE_REVISION=${SOURCE_REVISION} EP_BASE_IMAGE_ID=${BASE_IMAGE_ID}
ENV PYTHONDONTWRITEBYTECODE=1 ROS_LOG_DIR=/tmp/embodied-perf-ros-log
WORKDIR /app
COPY CMakeLists.txt ./
COPY src ./src
RUN /bin/bash -c 'source /opt/ros/${ROS_DISTRO}/setup.bash && cmake -S . -B build -DCMAKE_BUILD_TYPE=Release && cmake --build build -j2'
COPY perfkit ./perfkit
COPY tests ./tests
COPY configs ./configs
COPY scripts ./scripts
COPY Dockerfile ./Dockerfile
CMD ["python3", "-m", "perfkit.runner", "--config", "configs/smoke.json", "--output", "/results/run"]
