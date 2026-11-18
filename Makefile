PY := .venv/bin/python

.PHONY: setup run dry rescore serve clean help

help:
	@echo "make setup    创建虚拟环境并安装依赖"
	@echo "make run      完整运行（抓取 + 打分 + 生成网页）"
	@echo "make dry      试运行，不写任何文件"
	@echo "make nollm    只用关键词打分（不调用 LLM）"
	@echo "make rescore  按当前 lab.yml 重算已有文献"
	@echo "make serve    本地预览网页 http://localhost:8000"

setup:
	python3 -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

run:
	$(PY) scripts/run.py

dry:
	$(PY) scripts/run.py --dry-run

nollm:
	$(PY) scripts/run.py --no-llm

rescore:
	$(PY) scripts/run.py --rescore

serve:
	$(PY) -m http.server 8000 --directory docs
