# Logto OIDC 与后台 RBAC

Logto 提供登录身份和角色分配。Hyperswitch 仍负责本地用户、商户归属与 API 权限。每次 Logto 登录必须同时满足：已验证的 OIDC ID token、服务端 `sub` 与本地用户的显式绑定（旧静态绑定或启用后的持久化绑定）、Logto 角色声明、本地激活的相同角色及商户 ID。邮件地址相同不会自动关联账号，也不会自动创建用户或商户。

在 Logto 中创建传统 Web 应用，使用 Authorization Code + PKCE，精确回调地址为 `<dashboard-base-url>/redirect/oidc/logto`。客户端请求 `openid email roles urn:logto:scope:organization_roles`。在 Console > Custom JWT 启用 ID token 的 `roles` 和 `organization_roles` 声明。生产环境应使用 HTTPS，并给需要强认证的应用设置 MFA。Logto 文档：[自定义 ID token](https://docs.logto.io/developers/custom-id-token)、[组织角色](https://docs.logto.io/end-user-flows/organization-experience/get-user-info)。

Logto 全局角色名使用 `tenant_admin`。商户所在的 Hyperswitch Organization 对应一个 Logto Organization，在该组织内分配 `collection_merchant_admin` 或 `collection_merchant_view_only`。最终商户访问范围仍由持久化身份绑定中的本地商户 ID 限定；同一用户同时拥有多个受管商户归属会被当前登录检查拒绝。旧静态绑定由管理员在私有文件中指定。给商户角色只添加全局角色不会授予商户访问权限。

私有 JSON 示例（文件放在 Git 仓库外，权限设为 0600）：

```json
{
  "issuer": "https://identity.example.com/oidc",
  "client_id": "hyperswitch-dashboard",
  "client_secret": "<secret>",
  "owner_id": "<tenant-id>",
  "owner_type": "tenant",
  "email_domain": "example.com",
  "logto_bindings": {
    "<platform-sub>": {"user_id": "<local-platform-user-id>", "role_id": "tenant_admin", "organization_id": null, "merchant_id": null},
    "<merchant-sub>": {"user_id": "<local-merchant-user-id>", "role_id": "collection_merchant_admin", "organization_id": "<logto-org-id>", "merchant_id": "<local-merchant-id>"}
  }
}
```

`issuer` 必须与 Logto discovery 文档的 `issuer` 完全相同。配置服务端 `[user].base_url` 为后台网页的 origin。创建或更新认证方式：

```sh
HYPERSWITCH_API_URL=https://api.example.com HYPERSWITCH_ADMIN_KEY='<admin-key>' \
  python3 -B scripts/configure_logto_sso.py /private/path/logto.json

HYPERSWITCH_API_URL=https://api.example.com HYPERSWITCH_ADMIN_KEY='<admin-key>' \
  python3 -B scripts/configure_logto_sso.py /private/path/logto.json --update-id '<method-id>'
```

仅在隔离的内网调试环境中，如 Logto issuer 使用私有 IP 的 HTTP 地址，可显式附加 `--allow-http-private-dev`。正式部署不要使用此参数，应通过 HTTPS 域名暴露 Logto。

打开 `<dashboard-base-url>/?auth_id=<auth_id>` 登录。登录后仍签发普通 Hyperswitch 后台会话；API 继续校验本地 RBAC。为避免登录后切换到未获 Logto 授权的本地身份，Logto 绑定用户若有任何其他激活的本地角色或商户归属，登录会失败。需要多商户操作时应使用独立 Logto 账号及明确绑定。

Hyperswitch 是账号状态主数据源。停用本地用户或停用其受管管理员角色会通过 outbox 调用 Logto Management API 挂起对应的受管 Logto 用户；重新启用本地用户和角色后会解除挂起。Logto 挂起会阻止新的登录和 token，但不会自动撤销已经签发的 Hyperswitch 后台会话；紧急撤权仍应同时撤销其后台会话。此接入不从 Logto 反向创建用户或商户，也不接管支付连接器。

## 开户后单向同步（实验性，默认关闭）

Hyperswitch 是商户、用户和组织归属的开户来源。完成本地开户后，服务端才通过 Logto Management API 创建或关联对应的 Logto Organization、用户及其组织成员关系；用户在 Logto 自行注册或被直接创建，均**不得反向创建** Hyperswitch 商户或用户。现有 OIDC `allow_signup: false` 和 `sso_sign` 的显式绑定检查应继续保留。原先讨论的“新 Logto Organization 自动建商户，统一 US / USD”方案取消；新商户使用 Hyperswitch 开户时实际确认的业务资料，不能从 Logto 组织资料推断币种或渠道配置。

同步由本地角色写入事务触发持久化待办事件，后台工作进程幂等处理，而不是在 HTTP 请求内串行调用 Logto。组织和身份映射分别保存；待办代次防止过期工作进程覆盖新状态。创建 Logto 用户前按确定的本地用户 ID 查询并核对身份，绝不能只凭相同邮箱自动认领现有 Logto 账号。保存 `local_org_id ↔ logto_org_id`、`local_user_id ↔ logto_sub` 后，OIDC 登录可查持久化映射；静态 `logto_bindings` 用于现有账号迁移，若与数据库映射冲突则拒绝登录。

开户成功但 Logto 暂时不可用时，待办事件保持 `pending` 并指数退避重试；未建好持久化绑定的用户不能凭 Logto 身份进入后台。同步完成、组织成员关系和预期收款角色均核对后才写入 `ready` 绑定。开户自带的 `org_admin` 只同步普通组织成员身份，不自动赋予收款商户角色。开户本身不自动创建 API Key、渠道账户或额度池，也不开放收款；这些仍由平台审核配置。Logto 的角色分配继续作为登录授权声明，本地 RBAC 必须同时匹配。直接在 Logto 调整角色只影响已存在的绑定用户，不触发开户；角色撤销后的既有 Hyperswitch 会话仍需单独实现失效与角色同步，不能仅靠下次登录检查。

Management API 凭证只存服务端私有配置，并由专用 M2M 应用取得访问令牌；Logto 提供[服务端 M2M 接入说明](https://docs.logto.io/quick-starts/m2m)及[创建用户](https://openapi.logto.io/operation/operation-createuser)、[创建组织](https://openapi.logto.io/operation/operation-createorganization)接口。当前代码已包含全局用户库迁移、角色写入触发的持久化待办、单实例 Python 处理器及可选数据库绑定查询。隔离的远端调试环境已验证真实 Management API 创建、角色与组织绑定、幂等重放，以及 Control Center → Logto → OIDC 回调 → 指定商户首页的完整浏览器链路。生产部署仍需配置专用 M2M 应用并常驻运行处理器。平台管理员角色仍要求人工在 Logto 明确批准。撤权和旧会话失效尚未自动化。

迁移 `migrations/2026-10-21-000001_logto_account_sync` 和 `migrations/2026-10-25-000001_logto_user_status_sync` 应按顺序应用到**全局用户数据库**。`user_roles` 写入 `org_admin` 或受管角色、用户停用或重新启用时都会生成待办。服务端启用 `[platform].logto_identity_sync_enabled = true` 前，先确认迁移完成，并让处理器持续运行。处理器用私有 0600 JSON 配置启动：

```json
{
  "db_command": ["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1"],
  "management_api_base": "https://identity.example.com",
  "token_url": "https://identity.example.com/oidc/token",
  "management_api_resource": "https://default.logto.app/api",
  "m2m_client_id": "<server-side-id>",
  "m2m_client_secret": "<server-side-secret>"
}
```

数据库连接使用服务进程的 `PG*` 环境变量或受限的 PostgreSQL 服务文件，不能把密码写进命令参数。运行 `python3 -B scripts/logto_account_sync.py /private/logto-sync.json`；`--once` 只处理一个到期事件。Logto 临时错误会自动重试，身份冲突会留在待办表供平台排查。生产须单实例运行、记录告警并备份映射表。现有 Logto 用户要先安全迁移身份映射；处理器故意拒绝仅因邮箱相同而接管旧账号。

`management_api_base` 与 `token_url` 是两个独立地址。Logto OSS 使用独立管理端点时，机器令牌可能从管理端点签发，而 `/api/*` Management API 仍由核心端点提供；部署时应分别验证，不能假设两者同源。

新建 Logto 用户时不会复制 Hyperswitch 密码，也不会给用户设置 Logto 密码。同步器会生成不可猜测、稳定且与本地用户 ID 绑定的托管用户名，以兼容要求用户名资料的 Logto 策略；日常登录仍建议启用邮箱。若要让同步后的用户首次登录，须在 Logto 单独配置邮件验证码、邀请或其他身份验证方式；仅创建身份记录并不能完成实际登录。Logto 应设为 `SignIn` 模式以关闭自助注册，避免绕过 Hyperswitch 开户。迁移不会自动回填既有角色，试运行前需逐个核对旧账号的 `sub` 绑定。

## 自建 Logto 调试环境

使用 Logto 官方容器镜像和独立、持久化的 PostgreSQL 数据库。Logto 核心服务默认在容器内监听 3001，管理控制台监听 3002；设置 `DB_URL`、`ENDPOINT` 和 `ADMIN_ENDPOINT`，并把公开地址限定在可信内网或 HTTPS 反向代理。首次启动执行 `npm run cli db seed -- --swe`，随后运行 `npm start`。不要使用每次启动都会清空数据库的演示 Compose 配置。完成后检查 `<ENDPOINT>/oidc/.well-known/openid-configuration` 返回的 `issuer`，将其原样写入私有绑定文件。参考 [Logto OSS 部署文档](https://docs.logto.io/logto-oss/get-started-with-oss)。

在 Logto 默认租户创建 Traditional Web 应用，把回调地址设为 `<dashboard-base-url>/redirect/oidc/logto`。创建全局用户角色 `tenant_admin`、组织用户角色 `collection_merchant_admin` 与 `collection_merchant_view_only`，每个商户对应一个 Organization。为平台用户分配全局角色，为各商户用户只在所属 Organization 分配组织角色。通过 Console 的 Custom JWT 设置或 Management API `/api/configs/id-token` 启用 `roles` 和 `organization_roles`；应用登录请求也必须包含对应 scope。将三类用户的 Logto `sub` 与已有 Hyperswitch 用户 ID、角色、商户 ID 写入私有绑定文件，再运行上面的配置脚本。

本地调试时，Control Center 的 `config/feature` 必须给浏览器返回**浏览器可访问**的 `api_url`，不能返回服务器自己的 `localhost`。开发服务可通过 `default__endpoints__api_url=http://<zt-host>:<frontend-port>/api` 和 `API_PROXY_TARGET=http://127.0.0.1:<router-port>` 设置；后端 `[user].base_url` 应指向相同的前端 origin。上线时改为 HTTPS 域名，移除 `--allow-http-private-dev`。

验收至少覆盖：平台账号进入平台管理页，两个不同 Organization 的商户账号分别进入各自商户页；各商户的会话无法切换或读取另一个商户的数据；撤销 Logto 组织角色后，**新登录**不能再取得 Hyperswitch 会话。恢复角色后应重新验证登录。现有会话的撤销仍按上一段处理。
