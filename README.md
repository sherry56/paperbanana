# PaperBanana 轻量单体部署（FastAPI + Jinja2）

PaperBanana 提供“自动学术插图生成/对比”的后台服务。本仓库按**轻量单体架构**部署：
- Web 与模板：`FastAPI + Jinja2`
- 数据：`MySQL`（只负责业务数据：用户、额度/档位、订单/配置等）
- 文件：本地磁盘（只负责生成/下载/图库等文件；不把大文件塞进数据库）

线上部署不包含 `streamlit`：只运行 FastAPI。

## 架构概览

1. `FastAPI`：提供登录、管理后台、生成入口、下载导出等 HTTP 页面/接口。
2. `Jinja2`：服务端渲染 HTML 页面（`app/templates/`）。
3. `MySQL`：持久化用户与业务配置（`app/db.py` 与 `app/models.py`）。
4. 本地文件：生成候选图片、用户图库、任务状态清单等落在 `user_data/` 下。
   - 生成候选图：`user_data/results/<username>/<job_id>/candidate_*.png`
   - 生成任务状态：`user_data/results/<username>/jobs_manifest.json`
   - 用户图库：`user_data/gallery/<username>/manifest.json` + png 文件

## 本地启动（推荐）

### 1) 安装依赖

```bash
pip install -r requirements.txt
```

### 2) 配置环境变量（至少需要 MySQL）

```bash
PB_DB_HOST=127.0.0.1
PB_DB_PORT=3306
PB_DB_USER=root
PB_DB_PASSWORD=your_password
PB_DB_NAME=paperfigure

PB_ADMIN_USERNAME=B308
PB_ADMIN_PASSWORD=your_admin_password
```

### 3) 启动服务

```bash
python -m app.main
```

默认监听 `0.0.0.0:8000`。也可通过环境变量调整（用于容器/服务器对外访问）：
- `PB_HOST`：默认 `0.0.0.0`
- `PB_PORT`：默认 `8000`
- `PB_RELOAD`：`1/true` 启用热重载（生产建议关闭）

## Docker 部署

### 1) 构建镜像

```bash
docker build -t paperbanana:latest .
```

### 2) 运行容器

```bash
docker run --rm -p 8080:8080 \
  -e PB_DB_HOST=your_mysql_host \
  -e PB_DB_PORT=3306 \
  -e PB_DB_USER=your_mysql_user \
  -e PB_DB_PASSWORD=your_mysql_password \
  -e PB_DB_NAME=paperfigure \
  -e PB_ADMIN_USERNAME=B308 \
  -e PB_ADMIN_PASSWORD=your_admin_password \
  -v ./user_data:/app/user_data \
  -v ./data:/app/data:ro \
  -v ./configs:/app/configs:ro \
  paperbanana:latest
```

容器内启动命令为：`uvicorn app.main:app --host 0.0.0.0 --port 8080`。

## 支付宝配置（若启用计费）

如果需要支付宝支付，请在运行时注入以下环境变量（`PB_ALIPAY_ENABLED=1` 开启）：
- `PB_ALIPAY_ENABLED`
- `PB_ALIPAY_APP_ID`
- `PB_ALIPAY_GATEWAY`
- `PB_ALIPAY_NOTIFY_URL`（异步通知，公网 HTTPS）
- `PB_ALIPAY_RETURN_URL`（同步跳转）
- `PB_ALIPAY_APP_PRIVATE_KEY`
- `PB_ALIPAY_PUBLIC_KEY`
- `PB_ALIPAY_SELLER_ID`（推荐）

支付宝异步通知的处理遵循支付宝要求：回调返回纯文本 `success`，否则可能重复通知。

## License / Citation

Apache-2.0

```bibtex
@article{zhu2026paperbanana,
  title={PaperBanana: Automating Academic Illustration for AI Scientists},
  author={Zhu, Dawei and Meng, Rui and Song, Yale and Wei, Xiyu and Li, Sujian and Pfister, Tomas and Yoon, Jinsung},
  journal={arXiv preprint arXiv:2601.23265},
  year={2026}
}
```

