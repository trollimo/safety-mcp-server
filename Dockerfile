# Python 3.10+ обязателен: пакет mcp не поддерживает 3.8/3.9.
ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim
ARG MCP_UID=1000
ARG MCP_GID=1000
ARG APPROVER_UID=1001
ARG APPROVER_GID=1001
RUN apt-get update \
 && apt-get install -y --no-install-recommends openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd -g ${MCP_GID} mcp && useradd -u ${MCP_UID} -g ${MCP_GID} -m -s /usr/sbin/nologin mcp \
 && groupadd -g ${APPROVER_GID} approver && useradd -u ${APPROVER_UID} -g ${APPROVER_GID} -m -s /usr/sbin/nologin approver
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY safety_mcp/ safety_mcp/
COPY server.py approver.py ./
COPY wrapper/ wrapper/
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 SAFETY_CONF=/conf
USER mcp
ENTRYPOINT ["python", "/app/server.py"]
