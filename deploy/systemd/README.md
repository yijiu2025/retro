# 密钥配置说明（systemd 部署用）

systemd 单元文件里**不要**直接写密钥 —— 单元文件通常全局可读
（`systemctl cat` 任何用户都能执行），而且容易随包分发泄露。

## 做法

```bash
sudo mkdir -p /etc/retrochimera
sudo vi /etc/retrochimera.env
```

内容（权限必须是 600，只有 root 能读）：

```ini
RETRO_LLM_BASE_URL=https://api.deepseek.com/v1
RETRO_LLM_API_KEY=sk-xxxxxxxxxxxxxxxx
RETRO_LLM_MODEL=deepseek-chat
```

然后在两个单元文件的 `[Service]` 段加一行：

```ini
EnvironmentFile=/etc/retrochimera.env
```

最后收紧权限并重载：

```bash
sudo chmod 600 /etc/retrochimera.env
sudo chown root:root /etc/retrochimera.env
sudo systemctl daemon-reload
sudo systemctl restart retrochimera-api
```

## 另一种做法：直接用 web/.env

应用本身就会读 `web/.env`（见 web/llm_config.py 的 `_load_env_file`），
所以不配 `EnvironmentFile` 也能工作。此时要保证的是：

```bash
chmod 600 /srv/retrochimera/web/.env
```

但要注意：`ProtectHome=read-only` 这类加固项**不影响**读 `web/.env`，
可读即可；真正要防的是**其他用户能读**。

## 验证

```bash
# 看配置是否被识别（key 会脱敏）
curl -s http://127.0.0.1:8000/api/llm/status | python3 -m json.tool

# 真实请求一次（会消耗 token，别频繁跑）
curl -s "http://127.0.0.1:8000/api/llm/status?probe=1" | python3 -m json.tool
```

## 模型服务不需要密钥

`retrochimera-model.service` **不需要**任何 LLM 配置 —— 它只做化学推理，
与大模型完全隔离（LLM 走 API 进程，见 README 第 1 节的架构说明）。
所以 `EnvironmentFile` 只加到 API 那个单元即可。
