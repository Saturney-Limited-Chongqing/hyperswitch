# Logto OIDC 与后台 RBAC

Logto 提供登录身份和角色分配。Hyperswitch 仍负责本地用户、商户归属与 API 权限。每次 Logto 登录必须同时满足：已验证的 OIDC ID token、管理员配置的 `sub` 绑定、Logto 角色声明、本地激活的相同角色及商户 ID。邮件地址相同不会自动关联账号，也不会自动创建用户或商户。

在 Logto 中创建传统 Web 应用，使用 Authorization Code + PKCE，精确回调地址为 `<dashboard-base-url>/redirect/oidc/logto`。客户端请求 `openid email roles urn:logto:scope:organization_roles`。在 Console > Custom JWT 启用 ID token 的 `roles` 和 `organization_roles` 声明。生产环境应使用 HTTPS，并给需要强认证的应用设置 MFA。Logto 文档：[自定义 ID token](https://docs.logto.io/developers/custom-id-token)、[组织角色](https://docs.logto.io/end-user-flows/organization-experience/get-user-info)。

Logto 全局角色名使用 `tenant_admin`。商户创建为 Logto Organization，在该组织内分配 `collection_merchant_admin` 或 `collection_merchant_view_only`。每个 Logto Organization ID 对应一个本地商户 ID；该对应关系由管理员在私有绑定文件中指定。不同商户不能共用一个绑定。给商户角色只添加全局角色不会授予商户访问权限。

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

Logto 角色变更影响下次登录，不会自动撤销已经签发的 Hyperswitch 会话。紧急撤权应同时停用本地账号或撤销其后台会话。此接入不自动同步 Logto 用户或角色到数据库，也不接管支付连接器。

## 自建 Logto 调试环境

使用 Logto 官方容器镜像和独立、持久化的 PostgreSQL 数据库。Logto 核心服务默认在容器内监听 3001，管理控制台监听 3002；设置 `DB_URL`、`ENDPOINT` 和 `ADMIN_ENDPOINT`，并把公开地址限定在可信内网或 HTTPS 反向代理。首次启动执行 `npm run cli db seed -- --swe`，随后运行 `npm start`。不要使用每次启动都会清空数据库的演示 Compose 配置。完成后检查 `<ENDPOINT>/oidc/.well-known/openid-configuration` 返回的 `issuer`，将其原样写入私有绑定文件。参考 [Logto OSS 部署文档](https://docs.logto.io/logto-oss/get-started-with-oss)。

在 Logto 默认租户创建 Traditional Web 应用，把回调地址设为 `<dashboard-base-url>/redirect/oidc/logto`。创建全局用户角色 `tenant_admin`、组织用户角色 `collection_merchant_admin` 与 `collection_merchant_view_only`，每个商户对应一个 Organization。为平台用户分配全局角色，为各商户用户只在所属 Organization 分配组织角色。通过 Console 的 Custom JWT 设置或 Management API `/api/configs/id-token` 启用 `roles` 和 `organization_roles`；应用登录请求也必须包含对应 scope。将三类用户的 Logto `sub` 与已有 Hyperswitch 用户 ID、角色、商户 ID 写入私有绑定文件，再运行上面的配置脚本。

本地调试时，Control Center 的 `config/feature` 必须给浏览器返回**浏览器可访问**的 `api_url`，不能返回服务器自己的 `localhost`。开发服务可通过 `default__endpoints__api_url=http://<zt-host>:<frontend-port>/api` 和 `API_PROXY_TARGET=http://127.0.0.1:<router-port>` 设置；后端 `[user].base_url` 应指向相同的前端 origin。上线时改为 HTTPS 域名，移除 `--allow-http-private-dev`。

验收至少覆盖：平台账号进入平台管理页，两个不同 Organization 的商户账号分别进入各自商户页；各商户的会话无法切换或读取另一个商户的数据；撤销 Logto 组织角色后，**新登录**不能再取得 Hyperswitch 会话。恢复角色后应重新验证登录。现有会话的撤销仍按上一段处理。
