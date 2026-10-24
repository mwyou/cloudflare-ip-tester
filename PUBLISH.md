# GitHub → Docker Hub

源码保存在 GitHub，GitHub Actions 通过 Docker Hub Token 构建并推送镜像。域名、Cloudflare 凭据和用户自己的候选 IP 留在部署机器，不上传 GitHub、不进入镜像。

## 连接账号

先确认 GitHub 仓库地址、可见性和 Docker Hub 镜像仓库，例如 `OWNER/cloudflare-ip-tester` 与 `NAMESPACE/cloudflare-ip-tester`。下面的 `OWNER`、`NAMESPACE` 和 `USERNAME` 都需要替换为真实值。

在 GitHub 仓库 Settings → Secrets and variables → Actions 中设置：

| 类型 | 名称 | 内容 |
|---|---|---|
| Variable | `DOCKERHUB_USERNAME` | Docker Hub 登录用户名 |
| Variable | `DOCKERHUB_IMAGE` | 完整镜像名，例如 `NAMESPACE/cloudflare-ip-tester`，不含版本标签 |
| Secret | `DOCKERHUB_TOKEN` | 对目标镜像有推送权限的 Docker Hub Access Token |

不要把 Token 放在源码、工作流、Issues 或聊天中。可以直接在 GitHub 的 Secret 页面填写，或在你本机的交互终端中执行以下命令，由 GitHub CLI 提示输入 Token：

```sh
gh variable set DOCKERHUB_USERNAME --repo OWNER/cloudflare-ip-tester --body USERNAME
gh variable set DOCKERHUB_IMAGE --repo OWNER/cloudflare-ip-tester --body NAMESPACE/cloudflare-ip-tester
gh secret set DOCKERHUB_TOKEN --repo OWNER/cloudflare-ip-tester
```

Docker Hub 镜像仓库需要提前创建，并确认该 Token 有权向其推送。这个 Token 用于镜像发布，与部署时填写的 Cloudflare API Token 是两份不同凭据。

## 自动测试和发布

- 推送 `main` 或提交 Pull Request：运行 Python 回归测试、官方 CFST 契约测试、JavaScript 语法检查与 Compose 校验，再构建并运行 Linux amd64 / arm64 容器，检查非 root 用户、CFST 和跨容器数据卷持久化。不推送 Docker Hub。
- 推送 `v3.1.0` 这种正式版本标签：先通过上述检查，再构建并发布 `NAMESPACE/cloudflare-ip-tester:3.1.0` 与 `:latest` 两个标签。
- 手动发布：进入 GitHub Actions → Publish Docker Hub image → Run workflow，填写 `v3.1.0` 这种版本号。只接受 `v主版本.次版本.修订版本` 格式。

首次发布示例：

```sh
git tag v3.1.0
git push origin v3.1.0
```

工作流中的第三方 Action 已固定到具体提交 SHA。发布任务的 Docker Hub Token 不传入测试任务、不作为 Docker 构建参数。镜像中附带 CFST 的 GPLv3 许可及对应源码位置。

## 使用发布镜像部署

从仓库下载 `compose.hub.yaml` 与 `.env.example`，放在同一个部署目录。把 `.env.example` 复制为 `.env`，填写：

```dotenv
CF_DOMAIN=你的优选域名
CF_API_TOKEN=你的CloudflareToken
CFIP_IMAGE=NAMESPACE/cloudflare-ip-tester:3.1.0
```

然后运行：

```sh
mkdir -p config
docker compose -f compose.hub.yaml pull
docker compose -f compose.hub.yaml up -d
```

Windows PowerShell 可用 `New-Item -ItemType Directory -Force config` 创建目录。后续将 `CFIP_IMAGE` 改为新版本，再执行相同的 `pull` 和 `up -d` 即可更新网站。使用固定版本标签便于判断当前运行版本。

从源码构建切换为发布镜像时，保持相同部署目录、Compose 项目名、`.env` 及数据卷，即可沿用 `cfst-data` 与 `cfst-bin`。不要执行 `down -v`。

CFST 仍独立存储，拉取新网站镜像不会覆盖卷内选定的 CFST。使用发布版部署文件时，CFST 的查看、升级和回滚命令都需加 `-f compose.hub.yaml`，例如：

```sh
docker compose -f compose.hub.yaml run --rm --no-deps cfst-tools status
```

参考：[GitHub 发布 Docker 镜像](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)、[Docker Build and Push Action](https://github.com/docker/build-push-action)。
