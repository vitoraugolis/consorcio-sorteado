# Deploy no VPS Windows

## Pré-requisitos
1. **Python 3.12** — https://www.python.org/downloads/  
   Marque "Add Python to PATH" na instalação.
2. **Git** (opcional, para clonar o projeto) — https://git-scm.com/download/win
3. **.env** preenchido na raiz do projeto

## Instalação

1. Copie o projeto para `C:\consorcio-sorteado`
2. Copie o `.env` para `C:\consorcio-sorteado\.env`
3. Abra **PowerShell como Administrador**:
   ```powershell
   cd C:\consorcio-sorteado
   powershell -ExecutionPolicy Bypass -File windows\install_service.ps1
   ```

O script instala o NSSM, cria o venv, instala dependências e registra o servidor como serviço Windows com:
- **Início automático** no boot
- **Reinício automático** em caso de falha (10s)
- **Rotação de logs** a cada 10 MB

## Monitoramento

```powershell
# Ver status
nssm status ConsorcioSorteado

# Logs em tempo real
Get-Content C:\consorcio-sorteado\logs\server.log -Wait -Tail 50

# Reiniciar
nssm restart ConsorcioSorteado

# Health check
Invoke-RestMethod http://localhost:8000/health
```

## Webhook (ngrok no VPS)

Para expor o servidor ao Whapi/Z-API, instale o ngrok no VPS:
1. https://ngrok.com/download → Windows
2. `ngrok config add-authtoken <seu_token>`
3. `ngrok http 8000`
4. Atualize a URL do webhook no painel Whapi/Z-API com a URL pública

Para URL fixa (recomendado em produção): configure um domínio próprio com proxy reverso (Nginx/Caddy) + SSL.
