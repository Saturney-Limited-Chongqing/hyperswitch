# 后端交接：多商户收款中台

更新基线：Hyperswitch `82f79d492`（2026-10-25）；配套前端为独立仓库 `hyperswitch-control-center` 的 `frontend.md`（基线 `f922a1e`）。本仓库的 `cypress-tests/dashboard` 是测试报告，不是业务后台。本文记录相对上游的业务扩展、前端契约、构建和部署方法；具体协议与验证步骤以 `docs/` 和代码为准。

## 架构与权限边界

以 Hyperswitch Merchant 为一个收款商户，Business Profile 位于其内部；基础设施的 `multitenancy.tenants` 是另一层部署边界。平台管理员负责商户、支付渠道/连接器账户、路由及限额；收款商户只使用自己的收款、退款、客户、分析等资源。支付执行仍走现有原生支付 API、路由器和连接器，没有移植彩虹易支付支付插件。

新增本地角色 `collection_merchant_admin` 与 `collection_merchant_view_only`。前者可在商户范围管理支付、退款、客户、争议、报告、分析、成员、API 密钥及回调事件；后者限商户内读取。两者都不得查看/管理连接器、路由、代付、组织/租户管理。商户管理员只能邀请/分配这两种收款角色，不能给自己升级为旧 `merchant_admin`。平台角色 `tenant_admin`/`internal_admin` 保持平台能力。角色规则见 `crates/router/src/services/authorization/roles/predefined_roles.rs`、`core/user_role*`；完整矩阵见 `docs/collection_merchant_dashboards.md`。

仅授予后台角色**不足以**约束该商户的服务 API Key。还需在配置 `[platform].collection_merchant_ids` 列出所有收款商户；`crates/router/src/services/authorization/collection.rs` 在认证后、处理器执行前限制该类 Key 只能进入收款流程，并拒绝客户端提供渠道凭据或路由/连接器覆盖。商户 ID 必须从认证凭据取得，不能信任请求体。注册表默认空以兼容旧部署；新增商户必须同时完成成员授权和注册表配置。商户切换、订单/客户查询、API 密钥等均须按商户边界回归。

## OIDC：Logto 和 Rauthy

Logto 提供身份与角色声明；Hyperswitch 仍负责本地用户、商户归属和最终 RBAC。推荐配置：全局 `tenant_admin` 对应平台用户；每个商户一个 Logto Organization，组织内授予 `collection_merchant_admin` 或 `collection_merchant_view_only`。每个 Logto `sub` 都必须在服务端私有配置显式绑定到本地用户、角色、组织和商户；同邮件地址不能自动关联，不能自动注册商户。登录须同时通过 ID token、角色声明与本地激活角色检查；有多余本地角色/商户归属的绑定用户会拒绝登录。OIDC 使用 Authorization Code + PKCE；Logto 回调为 `<frontend-origin>/redirect/oidc/logto`，Rauthy 为 `/redirect/oidc/rauthy`。Rauthy 的角色依本地绑定，Rauthy 角色/组本身不授权。核心实现见 `crates/router/src/services/openidconnect.rs`、`core/user.rs`、`routes/user.rs`；配置工具见 `scripts/configure_logto_sso.py` 和 `scripts/configure_rauthy_sso.py`。

浏览器通过 `/?auth_id=<auth_id>` 选择认证方式。OIDC 成功后仍签发 Hyperswitch 后台会话。Logto 角色撤销影响**下一次登录**，不会自动废除已签发会话；紧急撤权须同时停用本地账户/撤销会话。不要把 OIDC client secret、管理员 Key、私有绑定文件或测试密码提交仓库。部署与精确声明、scope 见 `docs/logto_rbac.md`、`docs/rauthy_sso.md`。

## 渠道每日累计限额

V1/V2 支付执行路径可启用 `[platform].channel_daily_limits_enabled = true`；启用前必须运行 `migrations/2026-10-20-000001_channel_daily_limits`。策略键是 `(merchant_id, merchant_connector_id, currency)`；可用 `pool_id` 让多个账户共享同一额度池。限额、币种、IANA 时区、开关属于池，修改共享池会影响全部绑定渠道。`channel_daily_limits_require_binding = true` 时，未绑定的收款渠道/币种在调用渠道前被拒绝；启用前必须登记所有活动账户/币种。默认关闭该强制开关以兼容原有商户。V2 在最终执行点预留额度，但路由规划阶段暂不提前筛除额度已满的渠道。external-vault proxy 不支持该额度契约，详见 `docs/channel_limits_and_epay_v2.md`。

平台管理 API（挂在默认 `/api` 基路径下）：

| 方法/路径 | 用途 | 权限 |
| --- | --- | --- |
| `GET /configs/channel-limits` | 列出绑定、池策略和当日已用额（含预留） | 平台管理员 Key 或具 `TenantAccountWrite` 的后台 JWT |
| `POST /configs/channel-limits` | 新增绑定/更新限额 | 同上 |
| `POST /configs/channel-limits/reconcile` | 用证据将不确定预留确认/释放 | 同上 |

请求金额 `daily_limit` 为整数最小货币单位：USD 3000.00 为 `300000`。支付前在 PostgreSQL 原子预留；并发受池行锁约束，重试按商户/attempt/账户幂等。确认扣款后记入当日本地日期；退款**不恢复**收款额度。超时、网络错误、未知状态保留占额；不能在午夜或凭浏览器返回自动释放。未知结果需独立核对后调用 reconcile，附 `merchant_id`、`merchant_connector_id`、`attempt_id`、`state` 和有意义的 `reason`。存储/执行见 `crates/router/src/core/channel_limits.rs`、`db/channel_limits.rs`、`core/payments.rs`、迁移目录。前端限额页仅提供查看/保存，未提供 reconcile 页面；CLI 为 `scripts/channel_limits.py`。

## 彩虹兼容入口与退款

`services/epay_v2/` 是独立的 RSA-only **入站网关**，把指定的彩虹 V2 形式请求映射至绑定商户的 Hyperswitch 原生 API；它不是出站连接器。每个数字 `pid` 固定映射一个商户、Profile、原生 API Key 和币种。兼容范围：`GET/POST /api/pay/submit`、`POST /api/pay/create`、`POST /api/pay/query`、`POST /api/pay/refund`、`POST /api/pay/refundquery`。只支持 `sign_type=RSA`（缺省也视为 RSA），按字段名排序后用 RSA PKCS#1 v1.5 + SHA-256 签名；10 位秒级时间戳窗口 300 秒。旧 MD5 客户端、V1 路径、扫码/APP/小程序及商户列表 API 未实现，不应宣传兼容。商户私钥签请求，网关以注册公钥验签；响应和通知由平台私钥签，商户需验签。详细字段、签名规范和支持范围见 `docs/channel_limits_and_epay_v2.md`。

退款提交 `POST /api/pay/refund` 必须带 `trade_no` 或 `out_trade_no` 之一、正金额 `money`，建议带唯一 `out_refund_no`。它只对已捕获、属于该 PID 的付款生效，可多次部分退款但不得超过已捕获额。`out_refund_no` 在 PID 内唯一，同号同订单同金额重试复用原退款；不同参数报冲突。查询 `POST /api/pay/refundquery` 使用 `refund_no` 或 `out_refund_no` 之一。创建响应 `code=0` 仅表示请求被受理；`status=1` 才是成功，`status=0` 是确认失败，`status=2` 是待定/未知。调用方遇未知结果应查询或重试**同一退款号**，不得生成新号。网关以持久 SQLite WAL 保存订单、退款和通知状态；单份账本只运行一个网关实例，扩容前需改共享事务数据库/outbox。成功收款通知至少一次，商户按订单号去重；只有目标返回包含 `success` 的 2xx 才确认送达。浏览器 return 不是收款成功凭据。回调仅允许 HTTPS 公网地址，生产环境不能打开回环测试豁免。

原生退款仍使用 Hyperswitch `/refunds` 流程与现有连接器。兼容网关的密钥、账本路径和配置文件必须置于仓库外且权限受限；`services/epay_v2/config.example.json` 仅是模板。

`services/epay_v2/reconciliation.py` 增加离线对账和外部结算**记录**：按网关已确认收款/退款生成指定时间段的明细、总额、净额和摘要；人工核对银行或处理方账单后，可登记外部流水号及证据。记录不可修改，并拒绝同一商户重叠时间段。它不发起打款、不产生可提现余额，也不代表本系统持有资金；历史交易缺少可核验币种/退款确认时间时会拒绝生成报表。操作见 `docs/channel_limits_and_epay_v2.md`。彩虹入站接口仍只接受 RSA，新旧客户端均不能降级为 MD5。

## 以后新增支付 Connector（例如 Antom）

这里的 Connector 指 Hyperswitch 原生的**出站收单适配器**，与上面的彩虹 V2 入站网关不同。新渠道必须进入现有 `crates/hyperswitch_connectors`、路由和配置体系，不能让商户端传入直连渠道密钥，也不能绕开平台的商户边界及 V1 渠道限额。当前仓库随上游提供 `add_connector.md`，它是详细的接口/代码教程；以下是本项目的落地约定。

1. **先定义能力表。** 从渠道官方协议列出支持的币种、支付方式、认证方式（卡/钱包/跳转/3DS）、同步/异步结果、授权与扣款、撤销、部分退款、退款查询、webhook 验签与重放规则。写明 V1/V2 支持范围和沙箱/生产 API 差异。以 Antom 为例，先确定接入的具体 Antom 产品和地区 API；不要把品牌名当作单一通用协议。
2. **在独立、干净的工作树生成骨架。** 上游 `scripts/add_connector.sh <name> <sandbox-base-url>` 会修改枚举、注册表、配置、测试和模板文件，并在开头执行 `git checkout`/`rm -rf`，会覆盖相关未提交改动；它还会安装 `cargo-generate`、运行格式化/编译/测试。先阅读脚本、保存工作、确认工作树干净再运行。不要在日常有手工修改的 checkout 直接执行。脚本产物是起点，不是可上线的 Connector。
3. **实现协议转换。** 主体位于 `crates/hyperswitch_connectors/src/connectors/<name>.rs`，请求/响应与 `TryFrom` 转换位于 `.../connectors/<name>/transformers.rs`。实现 `ConnectorCommon`（`id`、base URL、认证头、错误响应、金额单位）、所需的 `ConnectorIntegration<Flow, Request, Response>` 和 `IncomingWebhook`。常见收款流包括 `Authorize`、`PSync`、`Capture`、`Void`、`Execute`/`RSync`（退款）；只声明并实现渠道实际支持的能力，不支持的流应显式失败。`crates/hyperswitch_connectors/src/connectors/gpayments.rs` 展示当前 trait 结构，但含未实现占位，不能照搬为完成品。
4. **注册和配置。** 核对 `crates/hyperswitch_connectors/src/connectors.rs`、`crates/router/src/connector.rs`、`crates/api_models/src/connector_enums.rs`、`crates/common_enums/src/connector_enums.rs`、`crates/hyperswitch_interfaces/src/configs.rs`、`crates/connector_configs` 和 `config/` 各环境文件；按支付方式声明可用性、凭据字段、webhook secret、沙箱/生产 base URL。具体注册点随上游版本变化，以生成后的 diff 和编译器报错为准。前端 Control Center 的展示名称、凭据表单、图标和必要的路由 WASM 也要适配，但只有平台管理员能创建/编辑渠道账户。
5. **把安全与结果语义做完整。** 用 `Secret`/masking 包装 API 密钥和敏感字段；签名与 webhook 必须验证原始请求、时间戳/事件 ID 和密钥轮换，不能凭收到 2xx 或浏览器跳转标记付款成功。映射渠道交易 ID、幂等键、错误码、待定状态、金额/币种精度、部分扣款和退款结果；超时不能盲目切换到别的渠道重扣。`handle_response` 和 `build_error_response` 要可区分确认失败与未知结果。新增渠道账户之后，再为需要限额的 `(merchant_id, merchant_connector_id, currency)` 配置额度池；限额不能凭 Connector 名称自动生效。
6. **逐层验收。** 在远端调试机用假服务器/协议样本覆盖签名、金额、错误和 webhook，再用渠道沙箱密钥跑真实授权、查询、退款和异步通知；执行对应 `crates/router/tests/connectors/<name>.rs`、Rust 编译、双商户隔离、渠道限额及前端平台/商户可见性回归。生产上线先由平台创建连接器账户、限定路由和日限额，再做小额真实交易与退款核对。沙箱与生产凭据都放私有配置，不能提交。

为了便于合入上游安全补丁，渠道差异尽量局限在新 Connector 文件、必要注册/配置项和前端对应项；不要改通用支付执行语义来适配单一渠道。每次 `update upstream` 后核对 trait、枚举、配置生成方式与 webhook 路径，再重新编译和回归。上游指南的行号和脚手架生成内容可能随版本变化，不应当成固定 API。

## 交给 AI 重写前端时的后端契约

新的中台前端可以使用任意框架（React、Vue、Svelte 等），不需要沿用 ReScript，但必须把本文件视为服务端契约。前端只负责展示、收集输入和调用 API；后端 RBAC、认证上下文和数据库隔离才是最终授权。建议先生成一个 API client 层，再写登录/会话层、角色路由守卫和页面，禁止在各组件中散落裸 `fetch`。

前端至少区分三类身份：平台管理员 `tenant_admin`/`internal_admin`、商户管理员 `collection_merchant_admin`、商户只读 `collection_merchant_view_only`。登录后先调用 `GET /api/user` 取得当前用户、角色、组织、商户和 Profile，再调用 `GET /api/user/role/v2` 取得资源权限。平台用户通过 `GET /api/user/list/merchant` 获取允许访问的商户；切换商户使用 `POST /api/user/switch/merchant`，收到新会话 token 后必须清空旧商户缓存、取消旧请求并重新加载用户资料。商户用户不能从 URL、localStorage 或请求体自行选择别的商户。V1 后端兼容当前 Control Center 使用的 `/api/v2/user/list/*` 与 `/api/v2/user/switch/*`，新前端优先使用不带 `/v2` 的 V1 后台路径，减少混用。

推荐页面分区如下：

| 区域 | 页面 | 后端规则 |
| --- | --- | --- |
| 公共认证 | 登录方式选择、OIDC 跳转、OIDC 回调、登出 | 认证方式来自 `/api/user/auth/list`；URL 来自 `/api/user/auth/url`；回调 POST `/api/user/oidc` |
| 平台管理 | 商户、成员、渠道账户、路由、渠道日限额、系统设置 | 只允许平台角色；渠道凭据永远不回显到浏览器 |
| 商户工作区 | 订单、退款、客户、争议、报告、分析、API Key、回调事件 | 当前会话所属商户；页面每次切换后重新取数 |
| 收银台 | 支付方式、SDK 容器、跳转/3DS 结果 | 只使用 publishable key 与单笔 `client_secret`，不使用后台 JWT 或 secret API key |

前端 API 基地址采用同源 `/api` 最稳妥：浏览器访问 `https://console.example.com`，反向代理把 `https://console.example.com/api/*` 转发到 Router 的 `/*`，即转发时去掉 `/api` 前缀；当前 Control Center 的 webpack proxy 也是这个规则。这样 OIDC cookie、CORS 和部署环境切换更容易控制。若前后端跨域，必须在 Router CORS 配置中精确允许前端 origin，启用凭据，并禁止通配 origin。`[user].base_url` 必须等于浏览器实际访问的前端 origin；Logto 回调必须精确登记为 `<origin>/redirect/oidc/logto`。

前端必须统一处理这些状态：401 清会话并回登录；403 显示权限不足而不是隐藏错误；404 对跨商户资源按不存在处理；409 显示幂等冲突；412 表示渠道额度或执行前置条件失败；5xx 保留请求 ID 供排查，支付/退款未知结果不得自动换单号重试。金额在 UI 可显示大单位，但 API 的 `amount`、`daily_limit` 等按对应接口要求使用整数最小货币单位；不得用 JavaScript 浮点数直接计算金额，使用整数或十进制定点库。

前端重写完成的最低验收：平台账号可以管理两个商户和渠道；两个独立商户账号只能看到自己的订单、客户、退款、API Key 和回调；商户直接访问平台 URL 同时被前端和后端拒绝；切换商户后无旧数据闪现；Logto 登录、登出、过期会话和错误页可用；零额度在调用真实渠道前被拒绝；支付与退款未知状态不会显示为成功。

## API、OpenAPI 与 SDK 接入教程

V1 是本项目当前生产契约。仓库中的 `api-reference/v1/openapi_spec_v1.json` 是生成前端类型和通用 API client 的起点；`api-reference/v2/openapi_spec_v2.json` 仅供明确选择 V2 的模块使用，不能把两个版本的模型任意混合。自定义的渠道额度接口、OIDC 扩展、彩虹 RSA 网关若尚未出现在 OpenAPI，需要按本文件及 `docs/` 手工补充 client 类型。生成后的 client 文件应固定在前端仓库并随本后端提交更新，CI 比较 OpenAPI hash 或生成结果，发现变化时要求人工审查。

认证凭据分四类，不能互换：

| 凭据 | 使用位置 | 是否可进入浏览器 |
| --- | --- | --- |
| 平台管理员 `api-key` | 建商户、建渠道、系统配置等受信任服务端操作 | 否 |
| 商户 secret API key | 商户服务端创建支付、退款、客户等 | 否 |
| 后台会话 Bearer token/cookie | 管理中台调用 `/user`、订单、退款等后台 API | 只由后台会话流程管理，不写入日志或 URL |
| Publishable key + 单笔 `client_secret` | 支付 Web SDK/收银台 | 是，但只能限制到对应商户和单笔支付 |

标准收银台流程必须由商户服务端开始：商户服务端使用 secret API key 调用 `POST /payments`，传入整数最小单位金额、币种、Profile、return URL 和业务订单标识；Router 返回支付 ID 与 `client_secret`。商户服务端只把 publishable key、支付 ID 和该笔 `client_secret` 交给浏览器。浏览器加载已部署并固定版本的 `HyperLoader.js`，以这些公开凭据挂载支付组件；SDK 再从 Router 读取可用支付方式并确认支付。支付完成页必须由商户服务端查询 `GET /payments/{payment_id}` 或消费已验签 webhook 后决定最终状态，不能相信浏览器 return URL 参数。

最小服务端请求示意（字段应以当前 OpenAPI 为准，示例值不能直接用于生产）：

```sh
curl -X POST "$HYPERSWITCH_API/payments" \
  -H "Content-Type: application/json" \
  -H "api-key: $MERCHANT_SECRET_API_KEY" \
  -d '{
    "amount": 1234,
    "currency": "USD",
    "confirm": false,
    "profile_id": "profile_example",
    "payment_id": "order_123",
    "return_url": "https://shop.example.com/payment/return"
  }'
```

浏览器只接收类似以下运行时配置，不能把 secret API key、管理员 Key、连接器密钥或 OIDC client secret 编译进 JavaScript：

```json
{
  "apiBaseUrl": "https://pay-api.example.com",
  "sdkUrl": "https://pay-sdk.example.com/0.16.7/v0/HyperLoader.js",
  "publishableKey": "pk_...",
  "paymentId": "order_123",
  "clientSecret": "..."
}
```

SDK 静态资源由独立 Web/SDK 构建产物提供，Router 的 `sdk_url` 只告诉页面去哪里加载脚本，不会自动托管 `HyperLoader.js`。生产必须使用 HTTPS、固定版本路径、正确的 CSP `script-src/connect-src/frame-src` 和缓存策略；升级 SDK 前在沙箱回归卡、钱包、跳转、3DS、取消和重复提交。中台后台本身不应加载收银台 SDK，除非实现专门的测试支付页面。

退款由受信任服务端或有权限的后台会话调用原生 `/refunds` API；浏览器不能持有商户 secret API key。创建退款必须提供原支付和幂等退款标识，随后查询退款状态或消费 webhook。彩虹兼容客户只连接独立 `services/epay_v2` 服务，按前述 RSA 协议调用，不能直接把其签名私钥交给浏览器。

## 后端构建教程

构建至少需要 Rust toolchain、PostgreSQL client headers、OpenSSL、`pkg-config` 和 `protoc`。以仓库锁文件为准，不要随意执行依赖大版本升级。常用检查：

```sh
cargo fmt --all -- --check
cargo check --locked -p router --bin router \
  --no-default-features --features v1,olap,oltp,redis-rs
cargo test --locked -p router \
  --no-default-features --features v1,olap,oltp,redis-rs
```

开发二进制：

```sh
cargo build --locked -p router --bin router \
  --no-default-features --features v1,olap,oltp,redis-rs
CONFIG_DIR="$PWD/config" \
ROUTER__SUPERPOSITION__BACKUP_FILE_PATH="$PWD/config/superposition_seed.toml" \
RUST_MIN_STACK=16777216 \
  target/debug/router -f /private/path/router.toml
```

生产优先构建不可变 Docker 镜像，不在生产主机现场编译：

```sh
docker build --pull --file Dockerfile \
  --build-arg BINARY=router \
  --build-arg VERSION_FEATURE_SET=v1 \
  --build-arg CARGO_BUILD_PROFILE=release \
  --tag registry.example.com/hyperswitch-router:<git-sha> .

docker build --pull --file Dockerfile \
  --build-arg BINARY=scheduler \
  --build-arg VERSION_FEATURE_SET=v1 \
  --build-arg CARGO_BUILD_PROFILE=release \
  --tag registry.example.com/hyperswitch-scheduler:<git-sha> .
```

根 Dockerfile 使用 `--no-default-features --features release --features <v1|v2>`；不要把同时启用 V1/V2 当成默认生产方案。镜像必须以 Git SHA 标记并保留上一版以便回滚。资源较少的机器构建很慢；建议至少 8 核、16GB 内存，CI 应缓存 Cargo registry/git 与 `target`，但发布镜像仍需从干净 checkout 构建。

### 按需编译渠道与身份提供方

仓库根目录的 `build.sh`、`test.sh` 使用同一套构建选择器。默认 `CONNECTORS=full` 保持上游所有渠道可用，默认 `IDENTITY_PROVIDERS=logto` 启用 Logto；精简部署以逗号分隔选择 `adyen,airwallex,checkout,paypal,stripe`。未选择的渠道不会进入 Router 的构造注册表，即使 TOML 中残留该渠道地址也无法创建或路由。Rauthy 需要通过 `IDENTITY_PROVIDERS=rauthy` 或 `logto,rauthy` 显式加入；未编译的 provider 名称无法反序列化为认证配置。若明确需要关闭全部外部身份提供方，应传入空值 `IDENTITY_PROVIDERS=`。示例：

```bash
# 只构建 Stripe + PayPal，并启用 Logto
CONNECTORS=stripe,paypal IDENTITY_PROVIDERS=logto ./build.sh

# 完整渠道，启用 Logto 与 Rauthy，并检查所有 target
CONNECTORS=full IDENTITY_PROVIDERS=logto,rauthy ./test.sh

# 内存充足的 CI 才启用所有测试/示例 target 检查
ALL_TARGETS=true CONNECTORS=full IDENTITY_PROVIDERS=logto,rauthy ./test.sh

# Docker 与 Cargo 使用相同 feature；精简生产镜像
docker build \
  --build-arg CONNECTOR_FEATURES=connector-stripe,connector-paypal \
  --build-arg IDENTITY_FEATURES=identity-logto \
  --build-arg VERSION_FEATURE_SET=v1 \
  -t hyperswitch-router:$(git rev-parse --short HEAD) .
```

渠道选择是编译期白名单而不是运行时开关。新增渠道时同时增加 `connector-<name>` feature、精简注册表分支和 `resolve_build_features.sh` 允许项；完整上游构建仍走 `connector-full`，从而减少长期合并冲突。OIDC 通用代码仍由 Router 共用，`identity-logto` 仅加入 Logto 角色/组织语义，`identity-rauthy` 仅加入 Rauthy 严格绑定语义。

本地依赖可用开发 Compose：

```sh
docker compose -f docker-compose-development.yml up -d \
  pg redis-standalone superposition superposition-init migration_runner
docker compose -f docker-compose-development.yml up -d hyperswitch-server
curl -fsS http://localhost:8080/health
curl -fsS http://localhost:8080/health/ready
```

开发 Compose 会从源码编译，适合首次联调而非生产。测试支付不得配置真实生产渠道。运行本项目定制回归时使用 `scripts/bootstrap_collection_test.py`、`test_collection_isolation.py`、`test_channel_limits.py`、`test_collection_payment_features.py`、`test_epay_refunds.py` 和 `services/epay_v2` 单测；夹具只允许指向 disposable 数据库。

## 数据库、配置与生产部署教程

生产至少准备 PostgreSQL、Redis、Router、反向代理；需要异步任务时部署 scheduler producer/consumer，需要 KV drainer 时按选定架构部署 drainer。Logto、Web SDK、Control Center 和彩虹兼容网关是独立服务，分别使用独立域名/路径、健康检查和发布版本。不要把 PostgreSQL、Redis 或内部管理端口暴露到公网。

部署顺序：

1. 从已审查提交构建并推送带 Git SHA 的 Router/Scheduler 镜像。
2. 备份数据库并确认可恢复；先在 staging 对相同版本备份演练迁移和回滚。
3. 使用与目标镜像同一提交的迁移目录运行 `DATABASE_URL=... just migrate`。本项目的渠道限额迁移在普通业务数据库；Logto 身份同步迁移必须应用到保存全局用户/角色的数据库。迁移任务只能单实例运行，成功后再启动新 Router。
4. 通过 secret manager 注入数据库密码、Redis 凭据、`master_enc_key`、JWT secret、管理员 Key、OIDC secret、KMS/对象存储凭据和连接器密钥。配置模板可提交，真实值不能写入镜像、Git、前端变量或日志。
5. 挂载只读生产配置，启动 Router：容器命令可显式使用 `/local/bin/router -f /run/secrets/router.toml`；也可用 `RUN_ENV`/`CONFIG_DIR` 和 `ROUTER__...` 环境变量覆盖。所有覆盖项在 staging 验证后才能进入生产。
6. 启动 scheduler、Logto 同步 worker、Web SDK、Control Center 和可选的 `epay_v2`。每个进程单独设重启策略、资源限制、结构化日志和告警。
7. 反向代理只公开 HTTPS API、后台、SDK及需要的 webhook；设置真实客户端 IP、请求体大小、超时和 HSTS。不要让代理缓存认证、支付、退款或 webhook 响应。
8. 检查 `/health` 与 `/health/ready`，再用测试商户完成创建未确认支付、查询、额度拒绝、退款模拟、双商户越权拒绝和 OIDC 登录。生产真实渠道只做经过批准的小额交易。

关键配置核对：

| 配置 | 要求 |
| --- | --- |
| `[server]` | 监听容器内部地址；公网只经反向代理 |
| 数据库/Redis | TLS、最小权限、连接池、备份与监控；凭据来自 secret manager |
| `[user].base_url` | 后台浏览器真实 HTTPS origin |
| `sdk_url` | 可公开访问、固定版本的 `HyperLoader.js` URL |
| `platform.collection_merchant_ids` | 登记所有受收款型 service API Key 限制的商户 |
| `channel_daily_limits_enabled` | 迁移和额度策略完成后才能开启 |
| `channel_daily_limits_require_binding` | 所有活动渠道/币种都登记后开启，防止未绑定绕过 |
| `logto_identity_sync_enabled` | 全局用户库迁移、M2M 和常驻 worker 均就绪后开启 |
| `superposition.backup_file_path` | 指向镜像中真实存在的 `config/superposition_seed.toml`，避免配置服务故障拖垮请求 |

推荐域名布局：`api.example.com` 给支付/API，`console.example.com` 给中台，`sdk.example.com` 给版本化 SDK，`auth.example.com` 给 Logto，`epay.example.com` 给 RSA 兼容网关。若中台使用同源代理，则 `console.example.com/api` 转发到 Router。所有回调和 issuer 必须使用最终 HTTPS 地址，不能在生产保留 ZeroTier IP、localhost 或 HTTP 开发豁免。

中台静态站点可采用以下 Nginx 结构；`proxy_pass` 末尾的 `/` 用于去掉浏览器请求中的 `/api/` 前缀：

```nginx
server {
    listen 443 ssl http2;
    server_name console.example.com;
    root /usr/share/nginx/html;

    location /api/ {
        proxy_pass http://router:8080/;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
        add_header Cache-Control "no-store" always;
    }

    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

新前端应生成静态 `dist/`，用多阶段 Dockerfile 在固定 Node 版本中执行 lockfile 安装、测试和生产构建，再把 `dist/` 复制到非 root 静态服务器镜像。推荐把公开环境放入启动时生成的 `/runtime-config.json`，并对该文件设置 `Cache-Control: no-store`，这样同一镜像可在 staging/production 使用不同 API、SDK 和 Logto public client ID；不要为了换域名重新注入任何 secret。`index.html` 和带内容 hash 的 JS/CSS 采用不同缓存策略：HTML 短缓存或不缓存，hash 资源长期 immutable。SPA 服务器必须将 `/redirect/oidc/logto`、`/redirect/oidc/rauthy` 及其他客户端路由回退到 `index.html`。

发布采用滚动或蓝绿方式。先部署兼容旧前端的后端，再部署新前端；数据库迁移必须向后兼容至少一个发布窗口。回滚应用时不得盲目回滚数据库迁移，先判断新写入数据与旧代码是否兼容。部署后观察 401/403/409/412/5xx、支付未知状态、退款待定、额度预留、数据库连接池、Redis、scheduler 积压和 webhook 重试。日志必须携带请求 ID、商户 ID和支付/退款 ID，但过滤密码、token、卡数据、API Key和连接器响应中的敏感字段。

## AI 前端交付清单

把以下文件一并交给负责重写前端的 AI：本文件、前端仓库 `frontend.md`、`api-reference/v1/openapi_spec_v1.json`、`docs/collection_merchant_dashboards.md`、`docs/channel_limits_and_epay_v2.md`、`docs/logto_rbac.md` 与 `docs/rauthy_sso.md`。要求其输出：路由/页面清单、角色矩阵、生成 API client、统一错误处理、环境变量模板、Dockerfile、反向代理示例、构建脚本、测试脚本和部署说明。

前端构建产物只能包含公开变量，例如 `PUBLIC_API_BASE_URL`、`PUBLIC_SDK_URL`、Logto public client ID；私密值只能存在于后端或部署 secret。CI 至少执行类型检查、单元测试、生产构建、依赖漏洞检查和浏览器双商户回归。部署后用两个不同商户账号验证缓存隔离，并检查浏览器 Network/Application 面板不存在 secret API key、管理员 Key、OIDC client secret 或连接器凭据。

## 联调、测试和升级

本项目的前端在另一个仓库；后端 checkout 内的 `control-center/` 是便于联调的嵌套 checkout，`scripts/setup_collection_control_center.sh`、`config/control_center_collection_revision` 和 `patches/control-center-collection.patch` 用于重现历史前端集成。交付前端开发应以独立前端仓库 HEAD 和其 `frontend.md` 为准。

远端调试机 `flan@10.100.100.22`：后端源码 `/home/flan/Projects/hyperswitch`，前端源码 `/home/flan/Projects/hyperswitch-control-center`；私有测试配置在 `/home/flan/Projects/hyperswitch-debug`。当前 Logto 后端监听远端 `127.0.0.1:18082`，前端供 ZeroTier 浏览器访问 `10.100.100.22:19001`，Logto core/Console 分别为 `:18092`/`:18093`。debug Router 处理后台商户列表时需要以 `RUST_MIN_STACK=16777216` 启动，避免未优化构建的 Actix 工作线程栈溢出；正式发布仍应使用 release 构建并做同一路径的负载回归。旧 Rauthy/原调试服务使用独立端口；这些地址不是生产合同。远端私有配置、夹具与数据库不进 Git。构建和测试继续在远端完成，避免本机安装 Redis/PostgreSQL 等依赖。

回归入口：`scripts/test_collection_isolation.py`（双商户隔离）、`scripts/test_channel_limits.py`（并发/跨日/共享池）、`scripts/test_collection_payment_features.py`（真实本地 API 与签名入口）、`scripts/test_epay_refunds.py`（回环模拟连接器退款）、`services/epay_v2/test_gateway.py`（协议/通知）、前端 `./test.sh` 及浏览器隔离测试。支付功能测试不应误连真实收单渠道；生产渠道上线前另做沙箱支付、webhook 和结算日核对。已验证的测试条件及局限详见 `docs/collection_merchant_dashboards.md`、`docs/channel_limits_and_epay_v2.md`。

本地定制是上游 Hyperswitch 之上的独立提交链；紧急上游补丁应先拉取上游提交、审查与上述扩展的冲突，再在隔离环境编译、运行上述回归和双商户浏览器测试，最后部署。特别审查 `services/authorization`、用户/OIDC、路由与支付完成钩子、退款 API、配置和数据库迁移。不要用整目录覆盖上游连接器。对上游安全补丁的跟进仍需人工判断适用性和部署，不存在自动零日保证。

## 当前未完成/限制

- V2 路由规划暂不提前筛除满额渠道，最终预留会拒绝超额；默认未绑定渠道仍可通过，需开启强制绑定开关并登记全部渠道。
- Logto 单向开户同步已有实验性实现：全局用户库迁移会为 `org_admin` 及受管角色写入待办，`scripts/logto_account_sync.py` 用服务端 M2M 凭证创建 Logto 组织/用户/成员。生产通过后台 API 停用管理员时，Hyperswitch 会立即将该用户加入 Redis JWT 黑名单；worker 同时挂起受管 Logto 用户，并吊销其全部当前 Logto session 与关联 grant/token。重新启用只允许重新登录，不复活旧会话。`[platform].logto_identity_sync_enabled` 控制 OIDC 是否查询持久化绑定，默认关闭。从 Logto 新增用户反向建档保持停用。生产前仍需部署专用 M2M 配置、旧身份迁移和常驻 worker；绕过后台直接改数据库不会写入 Hyperswitch Redis 黑名单。详见 `docs/logto_rbac.md`。
- 彩虹兼容仍是上文 RSA 子集，保持拒绝 MD5；已有离线对账和外部结算记录，没有资金余额、自动打款/代付账本或多实例网关高可用。
- 外部收单成功、完整 webhook、清结算时间与平台资金账本尚不能仅由现有测试证明；实际渠道必须逐个沙箱及生产小额验证。
- 前端的上游高级功能页面尚未全面简体中文化。
