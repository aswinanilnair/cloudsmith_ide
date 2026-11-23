ARG CODE_SERVER_VERSION=4.96.4
FROM codercom/code-server:${CODE_SERVER_VERSION}

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip python3-venv \
 && rm -rf /var/lib/apt/lists/* \
 # Users never get root: remove passwordless sudo (no-new-privileges blocks it anyway).
 && rm -f /etc/sudoers.d/nopasswd

USER coder
# Must exist in the image so a fresh named volume inherits coder's ownership.
RUN mkdir -p /home/coder/project
WORKDIR /home/coder/project

# Auth is disabled because the CloudSmith proxy authenticates every request.
ENTRYPOINT ["/usr/bin/dumb-init", "--", "/usr/bin/code-server"]
CMD ["--bind-addr", "0.0.0.0:8080", "--auth", "none", "--disable-telemetry", "--disable-update-check", "--disable-workspace-trust", "/home/coder/project"]
