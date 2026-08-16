# Cómo y dónde ejecutar esto

El proyecto son dos cosas con necesidades opuestas:

| | Backtester (Parte 1) | Bot de alertas (Parte 2) |
|---|---|---|
| Cuándo corre | Cuando vos lo corrés | Cada 5 min, solo |
| Qué necesita | Python en tu máquina | Algo prendido 24/7 |
| Costo | USD 0 | USD 0 a 24/mes según dónde |

**No tienen que vivir en el mismo lado.** Backtesteás en tu notebook; solo las
alertas necesitan estar siempre arriba.

---

## Parte 1 — Backtester, en tu máquina

### Requisitos

- **Python 3.10 o superior** (`python3 --version`). Hace falta 3.10 por la
  sintaxis de tipos `str | None`.
- Conexión a internet (yfinance descarga de Yahoo).
- **No hace falta ninguna API key.**

### Instalación

```bash
git clone -b claude/nasdaq-trading-backtest-alerts-rxyohg \
  https://github.com/vvvictoriagarcia/vvictoriagarcia.git
cd vvictoriagarcia/nq-trading-system

# Entorno virtual: evita romper el Python del sistema
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### Correrlo

```bash
python -m src.main                                    # NQ=F 5m, 55 días
python -m src.main --ticker ES=F --interval 15m       # otro contrato
python -m src.main --lookback 30 --no-cache           # rango más corto, sin caché
python -m src.main --list-strategies                  # qué estrategias hay
python tools/optimize.py                              # validación fuera de muestra
python -m pytest tests/ -q                            # que no se rompió nada
```

Los resultados quedan en `outputs/`: `trades.csv`, `equity.csv`,
`metrics.json`, `chart.png`.

### Si algo falla

| Error | Qué pasa |
|---|---|
| `ModuleNotFoundError: yfinance` | Falta activar el venv, o el `pip install` |
| `No module named src` | Estás fuera de `nq-trading-system/`. El `-m` necesita esa carpeta como raíz |
| `yfinance devolvió 0 velas` | Rate limit de Yahoo (429). Esperá un minuto |
| `SSLError` / `Connection reset` | Estás detrás de un proxy que re-termina TLS. Poné `YF_IMPERSONATE=chrome110` en tu `.env` |
| `RANGO FUERA DE LOS LÍMITES` | Pediste más historia de la que Yahoo guarda. El mensaje te dice las opciones |

> **Sobre `YF_IMPERSONATE`:** solo hace falta en redes corporativas o entornos
> tipo CI donde un proxy intercepta el TLS. En una máquina normal dejalo vacío.

---

## Parte 2 — Bot de alertas, en algo que no se apague

Un bot que chequea cada 5 minutos necesita una máquina despierta a las 10:35
de un martes. Si n8n corre en tu notebook y la cerrás, no hay alertas — y lo
peligroso es que **no te enterás**, porque el silencio se parece a "no hubo
señal". Elegí el hosting pensando en eso.

### Comparación

| Opción | Costo | Esfuerzo | Siempre arriba |
|---|---|---|---|
| **VPS + Docker** | ~USD 5/mes | medio | ✅ |
| **n8n Cloud** | ~EUR 24/mes | mínimo | ✅ |
| **Railway / Render** | ~USD 5/mes | bajo | ✅ |
| **Tu compu** | USD 0 | bajo | ❌ solo si está prendida |

---

### Opción A — VPS con Docker (recomendada)

Mejor relación precio/control. Hetzner (CX22, ~EUR 4/mes), DigitalOcean
(~USD 6/mes) o Contabo alcanzan de sobra: n8n con un workflow consume poco.

**1. Crear el servidor** con Ubuntu 22.04+ y entrar por SSH.

**2. Instalar Docker:**

```bash
curl -fsSL https://get.docker.com | sh
```

**3. Crear `docker-compose.yml`:**

```yaml
services:
  n8n:
    image: n8nio/n8n:latest
    restart: unless-stopped          # sobrevive reinicios del servidor
    ports:
      - "5678:5678"
    environment:
      # --- tu zona horaria: define cómo n8n interpreta los crons ---
      - GENERIC_TIMEZONE=America/Argentina/Buenos_Aires
      - TZ=America/Argentina/Buenos_Aires

      # --- el chat id que lee el nodo de Telegram ---
      - TELEGRAM_CHAT_ID=123456789

      # --- login básico: no dejes n8n abierto a internet ---
      - N8N_BASIC_AUTH_ACTIVE=true
      - N8N_BASIC_AUTH_USER=victoria
      - N8N_BASIC_AUTH_PASSWORD=una_password_larga_y_random

      # --- clave con la que n8n cifra las credenciales guardadas ---
      # Generala con: openssl rand -hex 32
      # Si la perdés, perdés las credenciales guardadas.
      - N8N_ENCRYPTION_KEY=pegá_acá_los_64_caracteres

      # --- purga ejecuciones viejas para que no crezca sin límite ---
      - EXECUTIONS_DATA_PRUNE=true
      - EXECUTIONS_DATA_MAX_AGE=168   # horas (7 días)
    volumes:
      - n8n_data:/home/node/.n8n     # sin esto perdés todo al actualizar

volumes:
  n8n_data:
```

**4. Levantarlo:**

```bash
docker compose up -d
docker compose logs -f n8n    # ver que arrancó bien
```

**5. Entrar** a `http://IP_DEL_SERVIDOR:5678`, importar
`n8n/workflow_twelvedata_telegram.json` y cargar las credenciales (ver
[`../n8n/README.md`](../n8n/README.md)).

> **Seguridad mínima:** el `N8N_BASIC_AUTH_*` no es opcional. Un n8n abierto
> en internet es un ejecutor de código con tus credenciales adentro. Lo ideal
> es además ponerle un reverse proxy con HTTPS (Caddy resuelve el certificado
> solo) o restringir el puerto 5678 por firewall a tu IP.

**Actualizar:**

```bash
docker compose pull && docker compose up -d
```

---

### Opción B — n8n Cloud

Si no querés administrar un servidor: [n8n.io/cloud](https://n8n.io/cloud),
plan Starter ~EUR 24/mes.

Importás el workflow y listo. **Un ajuste obligatorio:** el plan base no deja
definir variables de entorno propias, así que la expresión
`{{ $env.TELEGRAM_CHAT_ID }}` del nodo de Telegram no resuelve. Reemplazala
por tu chat id literal en el campo *Chat ID*.

Es el único valor que quedaría escrito en el workflow. No es tan sensible como
el token del bot (que sigue yendo en la credencial): con el chat id solo, sin
token, nadie puede escribirte.

---

### Opción C — Railway / Render

Punto medio: no administrás servidor pero tenés variables de entorno.

En Railway: **New Project → Deploy from Docker Image** → `n8nio/n8n`, agregás
las variables del compose de arriba y un volumen persistente en
`/home/node/.n8n`.

⚠️ Sin volumen persistente perdés workflows y credenciales en cada redeploy.

---

### Opción D — Tu computadora (solo para probar)

```bash
docker run -d --name n8n -p 5678:5678 \
  -e GENERIC_TIMEZONE=America/Argentina/Buenos_Aires \
  -e TELEGRAM_CHAT_ID=123456789 \
  -v n8n_data:/home/node/.n8n \
  n8nio/n8n
```

Entrás a `http://localhost:5678`.

**Solo sirve para probar el workflow.** Como bot real no: si cerrás la notebook
o se cae el wifi, dejás de recibir alertas sin ningún aviso.

---

## Parte 3 — Dashboard HTML en GitHub Pages

Una página con el estado del sistema que se regenera sola, sin que tengas
ningún servidor.

### Por qué GitHub Pages sí sirve, pero no como uno cree

Pages **solo sirve archivos estáticos**: no ejecuta Python ni nada del lado del
servidor. Lo que hace funcionar esto es la otra pata, **GitHub Actions**, que sí
ejecuta código en la infraestructura de GitHub según un cron.

```
┌─ cron ─┐   ┌─ runner de GitHub ──────────┐   ┌─ Pages ──────┐
│ cada   │──▶│ instala deps                │──▶│ sirve el     │
│ 15 min │   │ corre build_dashboard.py    │   │ index.html   │
└────────┘   │ yfinance → backtest → SVG   │   └──────────────┘
             └─────────────────────────────┘
```

El HTML se genera entero en build time: los gráficos son SVG escritos por el
script, no una librería de charts. La página no hace ni un pedido de red al
abrirse.

### ⚠️ Requisito previo: el workflow tiene que estar en la rama por defecto

GitHub **solo ejecuta los triggers `schedule` y `workflow_dispatch` de
workflows que estén en la rama por defecto** (acá, `main`). Es una regla de
GitHub, no una configuración: mientras `dashboard.yml` viva únicamente en la
rama de desarrollo, el cron nunca dispara y el botón "Run workflow" ni siquiera
aparece en la pestaña Actions.

Así que antes de nada, el archivo tiene que llegar a `main` — mergeando la rama
o abriendo un PR y aprobándolo.

### Activarlo

1. **Settings → Pages → Source: "GitHub Actions"** (una sola vez).
2. Listo. El workflow `.github/workflows/dashboard.yml` ya está en el repo.

Queda en `https://vvvictoriagarcia.github.io/vvictoriagarcia/`.

Para probarlo sin esperar al cron: **Actions → Dashboard → Run workflow**.

### Generarlo localmente

```bash
python tools/build_dashboard.py --out site
# abrí site/index.html en el navegador
```

Genera también `site/state.json` con el estado en JSON, por si querés
consumirlo desde otro lado.

### Qué muestra

- **Estado ahora:** último precio, señal vigente, RSI, spread de EMAs
- **Sparkline** del precio de las últimas 180 velas
- **Métricas del backtest** con las advertencias automáticas
- **Curva de equity** con el drawdown sombreado
- **Últimos 15 trades** con timestamps, puntos y motivo de salida
- **"Actualizado hace X"**, que se pone en rojo si pasaron más de 90 minutos

Ese último detalle importa: un panel congelado que no avisa que está congelado
es peor que no tener panel. Si el pipeline se corta, la página lo dice.

### Lo que NO hace, y por qué

**No es tiempo real.** Es una foto que se rehace cada 15 minutos. Para tiempo
real de verdad harían falta websockets y un servidor propio, y no vale la pena
para lo que muestra.

**No sirve para alertar.** El cron de GitHub Actions se retrasa entre 5 y 20
minutos, y más en horas pico — GitHub no garantiza puntualidad en el tier
gratuito. Por eso las alertas van por Telegram desde n8n, que sí corre a
horario, y el dashboard es para mirar, no para reaccionar.

Es la división correcta: **n8n te avisa, la página te muestra.**

### Costos y límites

| | |
|---|---|
| Actions en repos **públicos** | gratis, ilimitado |
| Actions en repos **privados** | 2.000 min/mes gratis (esto usa ~600) |
| GitHub Pages | gratis, 100 GB/mes de tráfico |

⚠️ GitHub **desactiva los workflows programados** si el repo pasa 60 días sin
actividad. Avisa por mail y se reactivan con un click.

### Sobre publicarlo en tu repo de perfil

Este workflow vive en `vvictoriagarcia/vvictoriagarcia`, que es tu repo de
perfil: público y visible en tu portfolio. Dos consideraciones:

- **A favor:** un panel que se actualiza solo es una buena demo de lo que hacés
  (datos + automatización). Suma más que un README.
- **A tener en cuenta:** este workflow **no commitea nada**. Publica un artifact
  directo a Pages, así que no ensucia el historial ni tu gráfico de
  contribuciones. Si en algún momento cambiás a un esquema que commitea el HTML,
  eso sí te llenaría el historial de commits automáticos.

Si preferís tenerlo aparte, creá un repo dedicado (`nq-dashboard`), moveleé la
carpeta y el workflow, y queda en
`vvvictoriagarcia.github.io/nq-dashboard/`.

---

## Sobre el horario y los crons

El cron del workflow está en **UTC** (`*/5 13-20 * * 1-5`) a propósito: así no
depende de la zona horaria del servidor. El recorte fino a 09:30–16:00 de Nueva
York lo hace el Code node, que maneja el cambio de hora de EE.UU. solo.

Por eso `GENERIC_TIMEZONE` no afecta a este workflow — pero conviene ponerlo
igual, para que los timestamps de las ejecuciones se lean en tu hora.

Argentina no tiene horario de verano y EE.UU. sí, así que la diferencia cambia
dos veces al año:

| Período | NY | Buenos Aires |
|---|---|---|
| Marzo–noviembre (EDT) | 09:30–16:00 | **10:30–17:00** |
| Noviembre–marzo (EST) | 09:30–16:00 | **11:30–18:00** |

---

## Signal service (opcional)

Solo si elegís la Opción B de [`../n8n/README.md`](../n8n/README.md) — mantener
una sola implementación de la estrategia en vez de dos.

```bash
pip install fastapi uvicorn
uvicorn src.signal_service:app --host 0.0.0.0 --port 8000
```

**Tiene que ser alcanzable desde n8n.** Si n8n está en un VPS y esto en tu
notebook, no se ven. Lo natural es correrlo en el mismo servidor, como un
segundo servicio del `docker-compose.yml`.

Si lo exponés, definí `SIGNAL_SERVICE_TOKEN` en el `.env`: sin esa variable el
endpoint queda abierto.

---

## Checklist antes de dejarlo andando

- [ ] `python -m pytest tests/ -q` pasa
- [ ] `python -m src.main` genera trades
- [ ] `python tools/check_parity.py` dice PARIDAD OK
- [ ] El workflow importado ejecuta a mano y devuelve `ok: true`
- [ ] Te llegó una alerta de prueba a Telegram
- [ ] Los parámetros del Code node coinciden con `config.yaml`
- [ ] `N8N_ENCRYPTION_KEY` guardada en algún lado seguro
- [ ] El volumen persistente está montado
- [ ] `restart: unless-stopped` puesto
- [ ] Verificaste al día siguiente que corrió sola

Ese último punto es el que más se saltea. Un bot de alertas falla en silencio:
si se rompió, lo que ves es exactamente lo mismo que si no hubo señales. Mirá
las Executions de n8n al día siguiente y confirmá que hay corridas cada 5
minutos en horario de mercado.
