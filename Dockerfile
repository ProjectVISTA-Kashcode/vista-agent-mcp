# Use the official Python image
FROM ghcr.io/astral-sh/uv:python3.13-trixie

# the user and group gitlab-runner needs to be created for evert app that requires GitOPS
RUN useradd -m gitlab-runner

# Define non root user for the created files
USER gitlab-runner
# Set the working directory
WORKDIR /app

# Optimal UV settings
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_NO_DEV=1
ENV UV_TOOL_BIN_DIR=/usr/local/bin

# Install dependencies using uv
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project


# Install the dependencies and python
#COPY pyproject.toml /app
#COPY uv.lock /app
#RUN uv sync --no-dev --frozen

# Copy the Flask app files
COPY --chown=gitlab-runner:gitlab-runner . /app
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked

# add .local to the path
RUN export PATH=/app/.venv/bin:$PATH

# make sure container user has ownership of app folder
# RUN chown -R gitlab-runner:gitlab-runner /app

ENTRYPOINT [ ]

# Expose the port or ports if the application has frontend/backend
EXPOSE 8100
