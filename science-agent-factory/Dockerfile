# syntax=docker/dockerfile:1.7
# 模块用途：构建可复现的 Science Agent Factory CLI 与 Harbor 运行镜像；不封装 Seed、运行产物或密钥。
# 主要接口：镜像入口 ``saf``；构建参数 ``VCS_REF``、``PYTHON_VERSION``、``UV_VERSION``、``DOCKER_CLI_VERSION`` 和 ``HARBOR_VERSION``。
# 输入：已提交源码的 40 位 Git Commit、项目 pyproject.toml、uv.lock、README.md 和 src/；构建时从固定基础镜像取得 Python、uv 与 Docker CLI。
# 输出：带 OCI Commit 标签、默认执行 ``saf --help`` 的非特权运行镜像。
# 副作用与边界：构建会访问软件源；镜像默认无 Docker Socket，若显式挂载 Socket 将获得宿主等价高权限。

ARG PYTHON_VERSION=3.12.12
ARG UV_VERSION=0.11.3
ARG DOCKER_CLI_VERSION=28.0.4

FROM docker:${DOCKER_CLI_VERSION}-cli AS docker_cli
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv_bin
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG HARBOR_VERSION=0.22.0

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    UV_TOOL_DIR=/opt/uv-tools \
    PATH=/opt/science-agent-factory/.venv/bin:/usr/local/bin:${PATH}

COPY --from=uv_bin /uv /uvx /usr/local/bin/
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/docker-compose \
    /usr/local/libexec/docker/cli-plugins/docker-compose

RUN apt-get update \
    && apt-get install --no-install-recommends --yes \
        asciinema \
        bash \
        ca-certificates \
        git \
        openssh-client \
        procps \
        tmux \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/science-agent-factory
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project \
    && uv tool install --python /usr/local/bin/python "harbor==${HARBOR_VERSION}" \
    && harbor --help >/dev/null

COPY README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev \
    && saf --help >/dev/null

RUN useradd --create-home --uid 10001 --shell /bin/bash saf \
    && mkdir -p /workspace \
    && chown saf:saf /workspace

ARG VCS_REF
RUN case "${VCS_REF}" in \
        ????????????????????????????????????????) ;; \
        *) echo "VCS_REF must be a 40-character Git Commit" >&2; exit 2 ;; \
    esac \
    && case "${VCS_REF}" in \
        *[!0-9a-f]*) echo "VCS_REF must contain lowercase hexadecimal characters" >&2; exit 2 ;; \
        *) ;; \
    esac

LABEL org.opencontainers.image.title="Science Agent Factory" \
      org.opencontainers.image.version="0.6.0" \
      org.opencontainers.image.revision="${VCS_REF}"

USER saf
WORKDIR /workspace
ENTRYPOINT ["saf"]
CMD ["--help"]
