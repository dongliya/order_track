# OrderFlow

一个基于 FastAPI 的轻量订单台账系统，用于集中维护订单、产品、发货、付款、发票和日志信息。

## 功能概览

- 用户登录与管理员后台
- 订单列表、筛选、新建、编辑、作废
- 订单详情统一维护：
  - 产品信息
  - 发货记录
  - 付款记录
  - 发票记录
  - 操作日志
- 产品资料管理
- 系统日志管理
- 合同附件本地上传与查看

## 技术栈

- FastAPI
- SQLAlchemy 2.x
- Jinja2
- SQLite

## 快速启动

```bash
cd order_track
python -m pip install -r requirements.txt
python run.py
```

启动后访问：

- `http://127.0.0.1:8000/login`

## 配置说明

默认数据库为项目目录下的 `order_track.db`。

如需自定义数据库连接，可设置环境变量：

```bash
export ORDER_TRACK_DATABASE_URL="sqlite:///./order_track.db"
```

## 目录结构

```text
order_track/
├── app/
├── static/
├── templates/
├── uploads/
├── requirements.txt
└── run.py
```

## 安全说明

公开上传 GitHub 前，建议至少检查以下内容：

- 不要提交真实业务数据、`order_track.db`、`uploads/` 上传文件。
- 不要提交包含真实账号、客户信息、合同文件的测试数据。
- 默认管理员账号仅适合本地演示，部署后请立即修改密码。
- 当前项目更适合内部工具或开发环境，正式上线前应替换会话密钥、补充环境变量配置和访问控制。

## 当前定位

这是一个偏内部使用的 MVP，适合快速搭建订单录入与跟踪流程，不包含复杂审批、第三方系统对接和生产级安全加固。
