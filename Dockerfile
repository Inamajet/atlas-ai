FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt tzdata
COPY main.py atlas.py ./
ENV PORT=5000
EXPOSE 5000
CMD ["python", "main.py"]
