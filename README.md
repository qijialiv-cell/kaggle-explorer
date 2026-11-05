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

## 登录

每个页面都要求登录：邮箱必须在 `allowed_emails` 白名单里，并输入共享的 `access_code`（都放在 secrets 里，不进代码库）。连续输错 5 次锁定 60 秒。收藏自动使用登录邮箱。加人 = 在 secrets 的 `allowed_emails` 里加一行。

## 部署到 Streamlit Community Cloud

1. 把本仓库 push 到 GitHub（建议 Private）。
2. 在 https://share.streamlit.io 用 GitHub 登录 → **Create app** → 选仓库、分支 `main`、主文件 `app.py`。
3. **Advanced settings → Secrets**：粘贴本地 `.streamlit/secrets.toml` 的全部内容（Kaggle token、`allowed_emails`、`access_code`）。
4. Deploy。首次启动会从 `data/seed_datasets.db.gz` 解压元数据缓存（含文件列表与描述），浏览时不再实时调用 Kaggle。

### 预缓存全部 Kaggle 元数据

```bash
python scripts/build_cache.py                  # 抓取列表 + 每个数据集的文件列表与描述（可断点续跑）
python scripts/build_cache.py --phase details  # 只补齐详情
python scripts/build_cache.py --export         # 生成 data/seed_datasets.db.gz，提交后推送即重新部署
```

脚本会自动限速以避开 Kaggle 的 429 限流；约 2.6 万个数据集的详情需要十几个小时，运行期间电脑不会自动休眠。

注意：Streamlit Cloud 的磁盘不持久——应用重启/休眠唤醒/重新部署后，SQLite 会回到 seed 状态，**收藏会丢失**。需要长期保存收藏时，把 bookmarks 表换成外部数据库（如 Supabase Postgres 或 Google Sheets）。

## 部署到内部服务器

- 服务器上同样 `pip install -r requirements.txt`，`streamlit run app.py --server.port 8501`，前面可加 Nginx / 学校 SSO。
- Kaggle token 只放在服务器的 `.streamlit/secrets.toml` 或环境变量里（`KAGGLE_API_TOKEN`）。
- 收藏没有登录系统，靠侧边栏填名字区分，适合小团队；需要权限控制时再加认证。
- 使用任何数据集前请在 Kaggle 页面确认 License；团队共用一个 Kaggle 账号前确认符合 Kaggle 条款与学校政策。

## Analyze 页面与 Gemini 研究助手

- **Analyze**：选一个数据集和其中的 CSV / Excel / JSON / Parquet 文件，应用会下载该文件并给出描述性统计（列类型、缺失值、均值/标准差/分位数/偏度/峰度/异常值、频数表、Pearson/Spearman 相关、分组比较），所有表格可导出 CSV。逻辑在 `services/analysis.py`。超过 150 MB 的文件不分析（可用 `max_analyze_mb` 调整）。
- **🤖 Ask Gemini** 标签页：粘贴自己的 Gemini API key（https://aistudio.google.com/apikey），即可就当前数据提问，或一键生成研究问题、假设、分析方案、数据质量检查、教学活动、论文提纲。逻辑在 `services/llm.py`。
  - Key 只保存在当前浏览器会话中，不写入磁盘、不共享，退出登录即清除。也可在 Secrets 中设置 `gemini_api_key` 供全组共用。
  - 默认只发送数据集描述和统计摘要；打开 “Also send the first 20 rows” 才会发送前 20 行原始数据。含个人或敏感信息的数据不要打开。
