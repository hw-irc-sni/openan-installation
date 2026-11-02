# Sample-A2A-T-Agent: A2A (a2a-sdk) + A2A-T (a2a-t-sdk) agent that answers every message with an LLM.
# Built by `make openan-sample-image` and loaded into kind (kind nodes cannot pull from Docker Hub).
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY agent.py .
USER 10001:10001
EXPOSE 8080
CMD ["python", "/app/agent.py"]
