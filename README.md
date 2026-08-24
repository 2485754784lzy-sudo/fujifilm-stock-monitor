# FUJIFILM Stock Monitor

云端监控 FUJIFILM 日本官网 20 款 instax 相纸库存。

- GitHub Actions 每 5 分钟运行一次
- 出现可用的「カートに入れる」按钮时判定为有货
- 仅在“缺货 → 有货”时发送提醒
- Telegram + 126 邮箱双提醒
- 首次运行只建立库存基线，不群发当前有货商品
- 403/429 时停止本轮，避免继续高频请求
- `unknown` 不覆盖上一次稳定库存状态
- 状态只在发生变化时写入 `state.json`

## GitHub Actions Secrets

在仓库中打开：

`Settings → Secrets and variables → Actions → New repository secret`

依次添加以下 4 个 Secret：

1. `TELEGRAM_BOT_TOKEN` — BotFather 提供的 Telegram Bot Token
2. `TELEGRAM_CHAT_ID` — 你的 Telegram Chat ID
3. `EMAIL_ADDRESS` — 126 邮箱地址
4. `EMAIL_APP_PASSWORD` — 126 邮箱客户端授权码（不是邮箱登录密码）

请勿把 Token、Chat ID 或邮箱授权码直接写进公开代码或 Issue。

## 手动测试通知

添加完 4 个 Secrets 后：

`Actions → FUJIFILM Stock Monitor → Run workflow`

把 `Run mode` 选择为 `test-notify`，再点击 `Run workflow`。

成功后应同时收到 Telegram 测试消息和 126 邮箱测试邮件。

## 手动执行一次库存检查

同一页面选择 `monitor` 后运行即可。首次监控只建立基线，不发送到货提醒。

## 自动运行

`.github/workflows/monitor.yml` 使用：

```yaml
cron: '*/5 * * * *'
```

即约每 5 分钟触发一次。GitHub Actions 的定时任务可能因平台负载出现少量延迟，并非严格整点执行。
