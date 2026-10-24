# GitHub → Docker Hub

源码保存在 GitHub，GitHub Actions 通过 Docker Hub Token 构建并推送镜像。域名、Cloudflare 凭据和用户自己的候选 IP 留在部署机器，不上传 GitHub、不进入镜像。

## 连接账号

本项目的公开源码仓库为 [mwyou/cloudflare-ip-tester](https://github.com/mwyou/cloudflare-ip-tester)，发布目标为 Docker Hub 镜像 `cotelforce/cloudflare-ip-tester`。

在 [GitHub Actions Secrets / Variables](https://github.com/mwyou/cloudflare-ip-tester/settings/secrets/actions) 中设置：

| 类型 | 名称 | 内容 |
|---|---|---|
| Variable | `DOCKERHUB_USERNAME` | `cotelforce` |
| Variable | `DOCKERHUB_IMAGE` | `cotelforce/cloudflare-ip-tester`，不含版本标签 |
| Secret | `DOCKERHUB_TOKEN` | 对目标镜像有推送权限的 Docker Hub Access Token |

不要把 Token 放在源码、工作流、Issues 或聊天中。可以直接在 GitHub 的 Secret 页面填写，或在你本机的交互终端中执行以下命令，由 GitHub CLI 提示输入 Token：

```sh
gh variable set DOCKERHUB_USERNAME --repo mwyou/cloudflare-ip-tester --body cotelforce
gh variable set DOCKERHUB_IMAGE --repo mwyou/cloudflare-ip-tester --body cotelforce/cloudflare-ip-tester
gh secret set DOCKERHUB_TOKEN --repo mwyou/cloudflare-ip-tester
```

Docker Hub 镜像仓库需要提前创建，并确认该 Token 有权向其推送。这个 Token 用于镜像发布，与部署时填写的 Cloudflare API Token 是两份不同凭据。

## 自动测试和发布

- 推送 `main` 或提交 Pull Request：运行 Python 回归测试、官方 CFST 契约测试、JavaScript 语法检查与 Compose 校验，再构建并运行 Linux amd64 / arm64 容器，检查非 root 用户、CFST 和跨容器数据卷持久化。不推送 Docker Hub。
- 推送 `v3.1.0` 这种正式版本标签：先通过上述检查，再构建并发布 `cotelforce/cloudflare-ip-tester:3.1.0` 与 `:latest` 两个标签。
- 手动发布：进入 GitHub Actions → Publish Docker Hub image → Run workflow，填写 `v3.1.0` 这种版本号。只接受 `v主版本.次版本.修订版本` 格式。

首次发布示例：

```sh
git tag v3.1.0
git push origin v3.1.0
```

工作流中的第三方 Action 已固定到具体提交 SHA。发布任务的 Docker Hub Token 不传入测试任务、不作为 Docker 构建参数。镜像中附带 CFST 的 GPLv3 许可及对应源码位置。

## 使用发布镜像部署

从仓库下载 `compose.yaml` 与 `.env.example`，放在同一个部署目录。把 `.env.example` 复制为 `.env`，填写域名和 Token。默认镜像为 `cotelforce/cloudflare-ip-tester:3.1.0`，可选的 `CFIP_IMAGE` 用于指定版本：

```dotenv
CF_DOMAIN=你的优选域名
CF_API_TOKEN=你的CloudflareToken
CFIP_IMAGE=cotelforce/cloudflare-ip-tester:3.1.0
```

然后运行：

```sh
mkdir -p config
docker compose pull
docker compose up -d
```

Windows PowerShell 可用 `New-Item -ItemType Directory -Force config` 创建目录。后续将 `CFIP_IMAGE` 改为新版本，再执行相同的 `pull` 和 `up -d` 即可更新网站。使用固定版本标签便于判断当前运行版本。

从源码构建切换为发布镜像时，保持相同部署目录、Compose 项目名、`.env` 及数据卷，即可沿用 `cfst-data` 与 `cfst-bin`。不要执行 `down -v`。

CFST 仍独立存储，拉取新网站镜像不会覆盖卷内选定的 CFST。默认部署可以直接运行维护命令，例如：

```sh
docker compose run --rm --no-deps cfst-tools status
```

旧的 `docker compose -f compose.hub.yaml ...` 命令仍可使用。需要源码构建时，叠加 `compose.build.yaml`：

```sh
docker compose -f compose.yaml -f compose.build.yaml up -d --build
```

参考：[GitHub 发布 Docker 镜像](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images)、[Docker Build and Push Action](https://github.com/docker/build-push-action)。
