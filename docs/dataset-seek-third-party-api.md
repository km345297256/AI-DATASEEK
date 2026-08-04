# AI-DataSeek 科学数据探查第三方接入接口文档

## 1. 文档说明

本文档用于指导第三方业务系统向 AI-DataSeek 提交服务器数据集目录，并将用户跳转至 `/dataset/seek/:datasetId` 页面开展科学数据探查分析。

- 接口版本：`v1`
- 示例服务地址：`http://39.106.98.67:7100`
- API 基础地址：`http://39.106.98.67:7100/api/v1`
- 请求及响应编码：`UTF-8`
- 请求数据格式：`application/json`

> `/dataset/setup` 页面仅用于人工联调和模拟第三方调用。它不是正式接入链路的前置步骤。第三方系统应直接调用本文档中的 API，然后自行把浏览器重定向到探查页面。

> `7100` 是 AI-DataSeek 的默认端口，用于与原系统的 `7000` 端口并行运行。实际接入时请统一替换为正式部署域名或地址。

> **安全边界：** AI-DataSeek 固定为免登录系统，不提供应用层调用者认证。任何能够访问服务的客户端都以同一个系统管理员身份操作。本文示例仅适用于可信网络；接入公网前必须在网络边界增加访问控制并启用 HTTPS。

## 2. 接入流程

三个地址的职责如下：

| 地址 | 用途 | 正式第三方接入是否需要 |
|---|---|---:|
| `/dataset/setup` | 人工联调页面，模拟提交接口并跳转 | 否 |
| `POST /api/v1/datasets/submissions` | 提交数据集信息并取得临时 `dataset_id` | 是 |
| `/dataset/seek/{dataset_id}` | 浏览器中的科学数据探查页面 | 是 |

1. 第三方服务直接调用数据集提交接口，不携带 AI-DataSeek 登录凭证或鉴权 Header。
2. AI-DataSeek 在 Docker 宿主机上解析并递归检查指定目录，生成一个临时 `dataset_id`。
3. 第三方服务读取响应中的 `data.dataset_id`。
4. 第三方服务将用户重定向至：

   ```text
   http://39.106.98.67:7100/dataset/seek/{dataset_id}
   ```

5. 页面加载数据集信息和四个简短的分析建议，并在用户发起分析时将整个目录只读挂载到沙箱。

提交接口只返回 JSON，**不会由 AI-DataSeek API 直接返回 302**。302 响应应由第三方 Web 服务生成；`/dataset/setup` 的表现等价于浏览器端收到 JSON 后执行前端路由跳转。

## 3. 重要接入约束

### 3.1 固定调用身份

AI-DataSeek 不提供账号登录，也不区分浏览器用户、第三方系统或脚本调用者。所有请求统一使用系统内置管理员身份：

- 第三方服务提交后，能够访问 AI-DataSeek 的浏览器可直接打开对应分析页面；
- 无需先登录，也无需在跳转 URL 中传递令牌；
- 系统不提供按调用者隔离的数据视图、权限或审计归属；
- `dataset_id` 是临时资源定位符，不是访问凭证，不能替代网络层访问控制。

由于所有调用共享管理员权限，部署方必须在 VPN、防火墙、反向代理或 API 网关层决定哪些客户端能够访问服务。

### 3.2 服务器目录

`storage_directory` 是 **AI-DataSeek 所在服务器上的目录**，不是第三方服务器或用户电脑上的目录。接口不会从第三方系统上传或复制文件。

默认配置允许的逻辑目录范围为：

- `/data`
- `/mnt`
- `/srv`
- `/opt/datasets`

实际范围以部署环境的 `DATASET_HOST_PATH_ALLOWLIST`（或执行节点的 `dataset_allowed_roots`）为准，第三方不能通过请求扩大该范围。

目录要求：

- 必须使用绝对路径；
- 路径中不得包含 `..` 或控制字符；
- 目录必须已经存在并可被 AI-DataSeek 读取；
- 系统递归识别普通文件；
- 符号链接、FIFO、Socket 等特殊文件不会作为数据文件展示；
- 单次提交默认最多递归识别 10,000 个普通文件；
- 分析时挂载整个目录，挂载权限固定为只读。

接口响应和分析页面只展示文件名，不返回或显示真实服务器存储路径。成功响应中的 `files[].path` 也已经脱敏为文件名，`locations` 固定隐藏为空数组；第三方不得依赖这两个字段恢复目录结构。

### 3.3 临时数据生命周期

提交元数据会在服务端数据库中临时保存，以保证 7100 后端重启后链接仍然可用；记录到期后自动清理。经安全校验的真实服务器目录只保存在服务端，不会出现在接口响应、URL、`localStorage` 或 `sessionStorage` 中；跳转 URL 中只包含不透明的临时 `dataset_id`，页面可能使用该 ID 在 `localStorage` 中关联当前分析会话。

- 临时数据集默认有效期：24 小时（当前版本不可通过该提交接口调整）；
- 服务最多保留 128 条临时数据集、每个调用方最多保留 16 条；达到上限后会先淘汰相应范围内最早创建的记录；
- 7100 后端服务重启不会使未过期的 `dataset_id` 失效；共享同一 MongoDB 的多个后端副本也可以读取同一条临时提交；
- 到达有效期后，读取接口会立即按已过期处理；MongoDB 的 TTL 清理任务随后删除服务端记录；
- 相同参数可以重复提交，每次都会返回新的 `dataset_id`；
- `external_id` 仅作为第三方业务标识回显，不做唯一性校验或幂等控制。

第三方系统应在提交成功后及时跳转；如保存临时 `dataset_id`，不得假定它在 24 小时有效期之外仍可访问。

### 3.4 Token 用量

模型 Token 只记录为运维和容量分析指标。系统不设置用户 Token 额度，不会因余额不足阻止任务，也不会从用户余额中扣减用量。

## 4. 调用方式与网络访问控制

AI-DataSeek 的应用接口固定免鉴权：

- 不需要登录；
- 不读取也不需要 `Authorization: Bearer ...`；
- 不读取也不需要 `X-API-Key`；
- 所有调用者自动获得同一个系统管理员身份。

因此，服务只能直接部署在可信网络中。需要跨网或公网访问时，必须先通过 VPN、防火墙 IP 白名单、mTLS、带认证的反向代理或 API 网关实施网络层访问控制，并使用 HTTPS。网关可以定义自己的认证 Header，但应在转发到 AI-DataSeek 前处理；这不属于 AI-DataSeek 的应用层鉴权。

## 5. 提交临时数据集

### 5.1 请求

```http
POST /api/v1/datasets/submissions
Content-Type: application/json
```

### 5.2 请求字段

| 字段 | 类型 | 必填 | 限制 | 说明 |
|---|---|---:|---|---|
| `external_id` | string | 是 | 1～200 字符 | 第三方业务系统中的数据集标识，可重复 |
| `name` | string | 是 | 1～300 字符 | 数据集名称 |
| `summary` | string | 是 | 1～4000 字符 | 数据集摘要，供页面展示及模型理解 |
| `keywords` | string[] | 是 | 1～100 项，每项最多 200 字符 | 数据集关键词；空项和重复项会被移除 |
| `storage_directory` | string | 是 | 1～4096 字符 | AI-DataSeek 服务器上的数据集目录绝对路径 |

请求体不允许额外字段。服务会去除三个文本字段、关键词项和目录字段首尾的空白；空关键词和重复关键词会被移除，处理后仍须至少保留一个关键词。

### 5.3 请求示例

```json
{
  "external_id": "7994ef4b-3c3a-48c1-8d85-8c2143d0f76a",
  "name": "祁连山国家公园年平均降水量（2011-2020）",
  "summary": "年平均降水量数据来源于 CRU_TS 数据集，经过投影、插值和边界裁剪后形成。",
  "keywords": ["降水", "降水量"],
  "storage_directory": "/data/A1"
}
```

### 5.4 cURL 示例

```bash
curl --request POST \
  'http://39.106.98.67:7100/api/v1/datasets/submissions' \
  --header 'Content-Type: application/json' \
  --data-raw '{
    "external_id": "7994ef4b-3c3a-48c1-8d85-8c2143d0f76a",
    "name": "祁连山国家公园年平均降水量（2011-2020）",
    "summary": "年平均降水量数据来源于 CRU_TS 数据集，经过投影、插值和边界裁剪后形成。",
    "keywords": ["降水", "降水量"],
    "storage_directory": "/data/A1"
  }'
```

> 当前接口接收 JSON，不接收 `application/x-www-form-urlencoded` 或 `multipart/form-data`。setup 页面中的可视化表单最终也是转换为上述 JSON 请求。

### 5.5 JavaScript（Node.js 18+）示例

推荐由第三方后端提交目录，避免把服务器目录配置暴露给普通浏览器。以下函数返回可用于 302 的探查地址：

```js
import express from 'express';

const app = express();
const AI_DATASEEK_BASE_URL = process.env.AI_DATASEEK_BASE_URL
  ?? 'http://39.106.98.67:7100';

async function submitDataset() {
  const response = await fetch(
    `${AI_DATASEEK_BASE_URL}/api/v1/datasets/submissions`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        external_id: '7994ef4b-3c3a-48c1-8d85-8c2143d0f76a',
        name: '祁连山国家公园年平均降水量（2011-2020）',
        summary: '年平均降水量数据，已完成投影、插值和边界裁剪。',
        keywords: ['降水', '降水量'],
        storage_directory: '/data/A1',
      }),
    },
  );

  const body = await response.json();
  if (!response.ok || body.code !== 0 || !body.data?.dataset_id) {
    throw new Error(body.msg ?? `AI-DataSeek HTTP ${response.status}`);
  }

  return new URL(
    `/dataset/seek/${encodeURIComponent(body.data.dataset_id)}`,
    AI_DATASEEK_BASE_URL,
  ).toString();
}

// Express 路由示例：第三方页面访问该路由后收到 302。
app.get('/datasets/qilian/open-in-ai-dataseek', async (_request, response, next) => {
  try {
    response.redirect(302, await submitDataset());
  } catch (error) {
    next(error);
  }
});
```

Express 的监听端口、错误处理中间件和进程生命周期由第三方项目现有启动代码负责。

### 5.6 Python 示例

```python
import os
from urllib.parse import quote

import requests
from flask import Flask, redirect

AI_DATASEEK_BASE_URL = os.getenv(
    "AI_DATASEEK_BASE_URL",
    "http://39.106.98.67:7100",
).rstrip("/")


def submit_dataset() -> str:
    response = requests.post(
        f"{AI_DATASEEK_BASE_URL}/api/v1/datasets/submissions",
        json={
            "external_id": "7994ef4b-3c3a-48c1-8d85-8c2143d0f76a",
            "name": "祁连山国家公园年平均降水量（2011-2020）",
            "summary": "年平均降水量数据，已完成投影、插值和边界裁剪。",
            "keywords": ["降水", "降水量"],
            "storage_directory": "/data/A1",
        },
        timeout=30,
    )
    body = response.json()
    if not response.ok or body.get("code") != 0:
        raise RuntimeError(body.get("msg", f"AI-DataSeek HTTP {response.status_code}"))

    dataset_id = body.get("data", {}).get("dataset_id")
    if not dataset_id:
        raise RuntimeError("AI-DataSeek 未返回 dataset_id")
    return f"{AI_DATASEEK_BASE_URL}/dataset/seek/{quote(dataset_id, safe='')}"


app = Flask(__name__)


@app.get("/datasets/qilian/open-in-ai-dataseek")
def open_in_ai_dataseek():
    return redirect(submit_dataset(), code=302)
```

生产代码还应设置连接超时、有限重试、结构化日志，并避免记录 `storage_directory`。

## 6. 成功响应

HTTP 状态码：`200 OK`

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "dataset_id": "tds_URLvjM3-64XUXzNkB8q0BDQZ",
    "external_id": "7994ef4b-3c3a-48c1-8d85-8c2143d0f76a",
    "data_center_id": "dataset-chat-demo",
    "data_center_name": "测试数据集",
    "name": "祁连山国家公园年平均降水量（2011-2020）",
    "description": "年平均降水量数据来源于 CRU_TS 数据集，经过投影、插值和边界裁剪后形成。",
    "temporal_coverage": "",
    "spatial_coverage": "",
    "data_type": "服务器目录",
    "tags": ["降水", "降水量"],
    "preview_url": "",
    "files": [
      {
        "name": "祁连山国家公园年平均降水量（2011-2020）.zip",
        "path": "祁连山国家公园年平均降水量（2011-2020）.zip",
        "size": 84921,
        "role": "data",
        "content_type": null
      }
    ],
    "metadata": {
      "temporary": true,
      "recursive_file_count": 1,
      "total_size_bytes": 84921
    },
    "locations": [],
    "enabled": true,
    "created_by": "anonymous",
    "created_at": "2026-08-03T13:34:42.000000Z",
    "updated_at": "2026-08-03T13:34:42.000000Z"
  }
}
```

第三方系统只需要依赖以下字段：

- `code`：业务状态，成功时为 `0`；
- `msg`：业务消息；
- `data.dataset_id`：构造跳转地址所需的临时数据集 ID。

其他响应字段可能随页面能力扩展而增加，第三方不应对未知字段报错。`data_center_id`、`data_center_name` 和 `created_by` 是当前内部兼容字段，不属于第三方接入契约。

## 7. 跳转到科学数据探查页面

提交成功后，对 `data.dataset_id` 执行 URL 编码并构造：

```text
http://39.106.98.67:7100/dataset/seek/{URL-encoded dataset_id}
```

示例：

```text
http://39.106.98.67:7100/dataset/seek/tds_URLvjM3-64XUXzNkB8q0BDQZ
```

第三方 Web 服务可以返回：

```http
HTTP/1.1 302 Found
Location: http://39.106.98.67:7100/dataset/seek/tds_URLvjM3-64XUXzNkB8q0BDQZ
```

伪代码：

```text
response = POST /api/v1/datasets/submissions

if response.http_status == 200 and response.body.code == 0:
    dataset_id = response.body.data.dataset_id
    redirect_url = AI_DATASEEK_BASE_URL + "/dataset/seek/" + url_encode(dataset_id)
    return HTTP 302 Location: redirect_url
else:
    display or record response.body.msg
```

不要把服务器目录或其他敏感业务信息拼接到跳转 URL 中。

如果第三方前端与 AI-DataSeek 部署在不同站点，302 的目标仍应使用浏览器可访问的 AI-DataSeek 公网域名或内网域名，而不是容器内部服务名。反向代理使用路径前缀时，需将该前缀同时纳入 API 基础地址和跳转地址。

## 8. 可选校验接口

第三方可以在跳转前确认临时数据集仍然有效：

```http
GET /api/v1/datasets/{dataset_id}
```

成功时的响应包装和 `data` 结构与提交接口相同；不存在、已过期或不属于当前调用方时返回 `404`。

页面会自动生成四个简短、面向数据分析与数据可视化的推荐问题。第三方通常不需要主动调用；如需预生成，可使用：

```http
POST /api/v1/datasets/{dataset_id}/suggested-questions
```

成功响应：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "questions": [
      "这个数据集包含哪些文件？",
      "数据质量怎么样？",
      "数据有哪些趋势或关系？",
      "如何进行数据可视化？"
    ]
  }
}
```

模型可能根据数据集名称、摘要、关键词和脱敏后的文件名微调措辞；模型生成失败时，接口使用上面四个短句作为兜底。返回数组固定为四项，第三方不应把具体文案作为稳定枚举值。

## 9. 错误响应

常规业务错误响应格式：

```json
{
  "code": 400,
  "msg": "Dataset source directory does not exist on the Docker host",
  "data": null
}
```

| HTTP 状态码 | 场景 | 处理建议 |
|---:|---|---|
| `400` | 目录不存在、不是目录、超出允许范围、目录过大或无法安全读取 | 记录 `msg`，检查服务器目录后重新提交 |
| `404` | 临时数据集不存在、已过期或不属于当前调用方 | 重新提交后再访问 |
| `422` | JSON 字段缺失、类型错误、长度超限或存在额外字段 | 根据响应中的 `detail` 修正请求体 |
| `500` | AI-DataSeek 内部服务异常 | 保留时间、请求参数和响应信息，联系系统管理员 |

FastAPI 请求体校验产生的 `422` 使用其标准 `detail` 数组格式，不一定包含上面的 `code/msg/data` 包装；调用方需要同时兼容两种错误结构。

第三方应同时检查 HTTP 状态码和响应体中的 `code`，不要仅根据 `msg` 文本判断成功。

如果部署方在反向代理或 API 网关配置了认证，网关可能额外返回 `401` 或 `403`；这类响应不由 AI-DataSeek 应用接口产生，应按网关策略处理。

## 10. 重试与幂等建议

当前接口不支持 `Idempotency-Key`，相同请求每次都会创建新的临时数据集。

- 收到明确的 `4xx` 响应时，不要原样自动重试；
- 收到 `5xx` 或网络超时时可以有限重试；
- 如果首次请求实际上已成功但响应在网络中丢失，重试会创建另一个 `dataset_id`；
- 第三方可以在自身系统中记录 `external_id`、请求时间和成功返回的 `dataset_id`，但不要把临时 ID 当作长期数据标识。

## 11. 安全建议

- AI-DataSeek 没有应用层登录或接口鉴权；任何可达客户端都拥有共享系统管理员权限；
- 服务只应直接暴露在可信网络，跨网或公网接入前必须配置网络层访问控制和 HTTPS；
- 推荐使用 VPN、防火墙 IP 白名单、mTLS 或带认证的反向代理/API 网关，并限制管理接口的网络可达范围；
- 不要把 `dataset_id` 当作安全令牌，也不要依赖难以猜测的 URL 保护数据；
- 不要在日志、URL 或浏览器存储中记录模型密钥、真实服务器目录或其他敏感配置；
- 第三方提交前应限制可填写的服务器目录范围，避免任意路径输入；
- AI-DataSeek 仍会使用 `DATASET_HOST_PATH_ALLOWLIST` 校验解析后的真实路径，部署方应将其限制为专用数据根目录；
- 分析沙箱中的数据目录固定为只读，模型产生的中间文件应写入沙箱其他工作目录。

## 12. 当前不支持的能力

- 通过该接口上传文件；
- 将第三方服务器本地路径直接挂载到 AI-DataSeek；
- 使用 HTML 原生表单直接提交 `urlencoded` 或 `multipart` 数据；
- 应用层账号登录、Bearer Token 或 `X-API-Key` 鉴权；
- 按调用者隔离资源、权限、审计归属或 Token 额度；
- 通过 `external_id` 查询或恢复之前的临时提交；
- 持久化保存临时数据集；
- 请求级幂等键。
