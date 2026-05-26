# Clark Email MCP Server
FROM python:3.12-slim
WORKDIR /app
COPY email_tool/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY email_tool/ .
EXPOSE 8080
CMD ["python", "server.py"]
