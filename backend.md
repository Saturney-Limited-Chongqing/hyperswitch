# 后端交接：多商户收款中台

更新基线：Hyperswitch `31bbeb0b2`（2026-10-21）；配套前端为独立仓库 `hyperswitch-control-center` 的 `frontend.md`（基线 `7e230e2`）。本仓库的 `cypress-tests/dashboard` 是测试报告，不是业务后台。本文记录相对上游的业务扩展和对接约束；具体协议与验证步骤以 `docs/` 和代码为准。

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

## 联调、测试和升级

本项目的前端在另一个仓库；后端 checkout 内的 `control-center/` 是便于联调的嵌套 checkout，`scripts/setup_collection_control_center.sh`、`config/control_center_collection_revision` 和 `patches/control-center-collection.patch` 用于重现历史前端集成。交付前端开发应以独立前端仓库 HEAD 和其 `frontend.md` 为准。

远端调试机 `flan@10.100.100.22`：后端源码 `/home/flan/Projects/hyperswitch`，前端源码 `/home/flan/Projects/hyperswitch-control-center`；私有测试配置在 `/home/flan/Projects/hyperswitch-debug`。当前 Logto 后端监听远端 `127.0.0.1:18082`，前端供 ZeroTier 浏览器访问 `10.100.100.22:19001`，Logto core/Console 分别为 `:18092`/`:18093`。旧 Rauthy/原调试服务使用独立端口；这些地址不是生产合同。远端私有配置、夹具与数据库不进 Git。构建和测试继续在远端完成，避免本机安装 Redis/PostgreSQL 等依赖。

回归入口：`scripts/test_collection_isolation.py`（双商户隔离）、`scripts/test_channel_limits.py`（并发/跨日/共享池）、`scripts/test_collection_payment_features.py`（真实本地 API 与签名入口）、`scripts/test_epay_refunds.py`（回环模拟连接器退款）、`services/epay_v2/test_gateway.py`（协议/通知）、前端 `./test.sh` 及浏览器隔离测试。支付功能测试不应误连真实收单渠道；生产渠道上线前另做沙箱支付、webhook 和结算日核对。已验证的测试条件及局限详见 `docs/collection_merchant_dashboards.md`、`docs/channel_limits_and_epay_v2.md`。

本地定制是上游 Hyperswitch 之上的独立提交链；紧急上游补丁应先拉取上游提交、审查与上述扩展的冲突，再在隔离环境编译、运行上述回归和双商户浏览器测试，最后部署。特别审查 `services/authorization`、用户/OIDC、路由与支付完成钩子、退款 API、配置和数据库迁移。不要用整目录覆盖上游连接器。对上游安全补丁的跟进仍需人工判断适用性和部署，不存在自动零日保证。

## 当前未完成/限制

- V2 路由规划暂不提前筛除满额渠道，最终预留会拒绝超额；默认未绑定渠道仍可通过，需开启强制绑定开关并登记全部渠道。
- Logto 角色撤销不自动撤销已有 Hyperswitch 会话；没有自动同步用户、角色或商户。后续方向改为 Hyperswitch 开户完成后单向同步组织/用户到 Logto；从 Logto 新增用户反向建档保持停用。原先讨论的反向建商户 US / USD 默认值不再适用。待办队列、持久化身份映射、动态收款权限登记和撤权机制尚未实现；详见 `docs/logto_rbac.md`。
- 彩虹兼容仍是上文 RSA 子集，保持拒绝 MD5；已有离线对账和外部结算记录，没有资金余额、自动打款/代付账本或多实例网关高可用。
- 外部收单成功、完整 webhook、清结算时间与平台资金账本尚不能仅由现有测试证明；实际渠道必须逐个沙箱及生产小额验证。
- 前端的上游高级功能页面尚未全面简体中文化。
