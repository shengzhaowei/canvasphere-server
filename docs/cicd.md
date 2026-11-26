# Canvasphere Server CI/CD 设置指南

推代码 → GitHub Actions 自动跑后端测试（pytest）→
只有 push 到 `main`（或手动 Run workflow）且测试通过才自动部署到首尔服务器（传包 + 重建容器 + 验 health）。

部署目标是服务器上的 `/opt/canvasphere`（与旧版同一目录、同一 compose 项目名，`pgdata` 数据库卷得以保留；旧版的 web 容器会成为孤儿容器，仍在跑，待新前端上线后再手动停掉）。

## 一、仓库 Secrets（在 canvasphere-server 仓库里填，绝不要发到聊天里）

仓库 Settings → Secrets and variables → Actions，新建：

| 名字 | 值 |
|---|---|
| `DEPLOY_HOST` | `8.220.194.80` |
| `DEPLOY_USER` | `admin` |
| `DEPLOY_SSH_KEY` | 部署专用 SSH 私钥（见第二节，只用于这台服务器） |

## 二、服务器部署钥匙（已建好，一般不用重做）

2026-11-26 已在服务器上生成部署专用钥匙 `/home/admin/.ssh/canvasphere_deploy`
（ed25519，指纹 `SHA256:YMIwfSqTgSKTAJ9GPEY49De8hJXPeqKmCg3wiH+1ck4`），
公钥已装进 admin 的 `authorized_keys`，admin 已在 docker 组，`/opt` 和
`/opt/canvasphere` 已归 admin 所有。私钥只在服务器上，绝不要发到聊天里。

如果钥匙丢了才重建（在 Workbench root 终端跑）：

```bash
sudo -u admin ssh-keygen -t ed25519 -f /home/admin/.ssh/canvasphere_deploy -N "" -C "canvasphere-deploy"
cat /home/admin/.ssh/canvasphere_deploy.pub >> /home/admin/.ssh/authorized_keys
chmod 600 /home/admin/.ssh/authorized_keys
chown admin:admin /home/admin/.ssh/authorized_keys
sudo usermod -aG docker admin
sudo chown -R admin:admin /opt/canvasphere
sudo chown admin:admin /opt
su - admin -c "docker ps"
```

## 三、验证

随便推一个提交到 `main`（或 Actions 页 → CI/CD → Run workflow），看 test 和 deploy
两步是否都绿，再打开 `http://8.220.194.80:8000/health` 核对。

## 四、前端换 Vercel 后的待办

- 把 Vercel 域名加进服务器 `/opt/canvasphere/.env` 的 `CORS_ORIGINS`，重启 api，否则浏览器跨域调不到后端。
- 新前端上线并验证后，停掉旧版 web 容器（`docker stop` + 防火墙关 5173）。
