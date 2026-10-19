FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    make bash coreutils poppler-utils git jq \
    && python -m pip download --only-binary=:all: --dest /wheels \
       setuptools==84.0.0 wheel==0.45.1 pillow==12.3.0 sqlite-vec==0.1.9
ENV PIP_NO_INDEX=1 PIP_FIND_LINKS=/wheels PIP_DISABLE_PIP_VERSION_CHECK=1
