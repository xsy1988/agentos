.PHONY: db-up db-down db-logs dev migrate upgrade test lint fmt hooks clean web dev-web

db-up:            ## 启动 PostgreSQL（pgvector）
	docker-compose up -d

db-down:          ## 停止数据库（数据保留在 volume）
	docker-compose down

db-logs:          ## 跟踪数据库日志
	docker-compose logs -f db

dev:              ## 启动后端开发服务（热重载，:8000）
	cd backend && uv run uvicorn app.main:app --reload --port 8000

migrate:          ## 生成迁移：make migrate m="描述"
	cd backend && uv run alembic revision --autogenerate -m "$(m)"

upgrade:          ## 应用迁移到最新
	cd backend && uv run alembic upgrade head

test:             ## 运行测试
	cd backend && uv run pytest

lint:             ## ruff + mypy 检查
	cd backend && uv run ruff check . && uv run mypy app

fmt:              ## 格式化并自动修复
	cd backend && uv run ruff format . && uv run ruff check --fix .

hooks:            ## 安装 git pre-commit 钩子（首次执行一次）
	cd backend && uv run pre-commit install

clean:            ## 清理缓存
	cd backend && rm -rf .pytest_cache .mypy_cache .ruff_cache

web:              ## 启动前端开发服务（热重载，:5173）
	cd frontend && npx vite --port 5173

dev-web:           ## 同时启动前后端（需两个终端）
	@echo "终端1: make dev  (后端 :8000)"
	@echo "终端2: make web  (前端 :5173)"
