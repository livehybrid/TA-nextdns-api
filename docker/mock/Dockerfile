FROM python:3.12-slim
COPY mock_nextdns.py /mock_nextdns.py
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/__mock/health')" || exit 1
CMD ["python", "/mock_nextdns.py"]
