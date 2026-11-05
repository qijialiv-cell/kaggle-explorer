# Kaggle Research Dataset Explorer (v1.0)

团队内部（2–5 人）使用的 Kaggle 数据集浏览器：搜索、筛选、查看详情、收藏、导出 CSV。
技术栈：Python + Streamlit + Kaggle API + SQLite + Pandas。

## 快速开始

```bash
# 1. 创建虚拟环境（建议放在 OneDrive 之外，避免同步大量小文件）
uv venv --python 3.12 ~/.venvs/kaggle-explorer
uv pip install --python ~/.venvs/kaggle-explorer -r requirements.txt
#   公司/学校网络如果报 "invalid peer certificate"，先设置：  set UV_NATIVE_TLS=1

# 2. 配置 Kaggle 凭证
copy .streamlit\secrets.toml.example .streamlit\secrets.toml   # 然后填写 token
#   或者把 kaggle.json 放到 ~/.kaggle/

# 3. 运行
~/.venvs/kaggle-explorer/Scripts/streamlit run app.py
```

打开 http://localhost:8501 → **Admin** → **Test connection** → **Refresh**。

## 页面

| 页面 | 功能 |
| --- | --- |
| Home | 缓存数量、收藏数、上次刷新时间 |
| Explore | 关键词搜索（本地缓存），License / File format / Size / Tags 筛选，按热度、下载、更新时间等排序，导出 CSV，“Search Kaggle live” 直接查 Kaggle 并写入缓存，选中一行查看详情 |
| Dataset detail | 在 Explore / Bookmarks 中选中一行后显示：描述、文件列表、Owner、License、Kaggle 链接、收藏（可写备注）|
| Bookmarks | 按人查看团队收藏，导出 CSV |
| Admin | 测试连接、手动刷新元数据、刷新历史；可用 `admin_password` 保护 |

## 数据怎么来的

- **Refresh** 对每个关键词 × 每种格式（all / csv / json / sqlite / parquet / bigQuery）× N 页调用 Kaggle `dataset_list`，只拉元数据，不下载数据文件。
- Kaggle 列表接口不直接返回文件格式，所以按格式分别查询来给数据集打上格式标签；打开详情时还会根据文件扩展名补充格式（Excel、Images 等）。
- 详情（文件列表 + 完整描述）在第一次打开时向 Kaggle 拉取，之后从 SQLite 读取。

## 目录

```
app.py                 首页
ui.py                  共享 UI：侧边栏、结果表、详情面板、收藏
config.py              设置（环境变量 → secrets.toml）
pages/                 Explore / Bookmarks / Admin
services/models.py     通用 Dataset Schema + Adapter 接口
services/kaggle_api.py Kaggle Adapter
services/dataset_service.py  同步、搜索、收藏
database/schema.sql    SQLite 表结构
data/datasets.db       运行后自动生成
```

## 以后扩展（Hugging Face / UCI）

1. 在 `services/` 新建 `hf_api.py`，继承 `DatasetAdapter`，实现 `search / get_files / get_description`，返回 `DatasetRecord(source="huggingface", ...)`。
2. 在 `dataset_service.get_adapter()` 注册它。
3. 表结构不用改：`datasets.id` 已经是 `"<source>:<ref>"`。

## 部署到内部服务器

- 服务器上同样 `pip install -r requirements.txt`，`streamlit run app.py --server.port 8501`，前面可加 Nginx / 学校 SSO。
- Kaggle token 只放在服务器的 `.streamlit/secrets.toml` 或环境变量里（`KAGGLE_API_TOKEN`）。
- 收藏没有登录系统，靠侧边栏填名字区分，适合小团队；需要权限控制时再加认证。
- 使用任何数据集前请在 Kaggle 页面确认 License；团队共用一个 Kaggle 账号前确认符合 Kaggle 条款与学校政策。
