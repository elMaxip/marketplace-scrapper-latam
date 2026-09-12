# AI Marketplace Monitor

Monitor de avisos para Chile y Latinoamérica. Busca productos en **Facebook
Marketplace**, **Mercado Libre**, **Lider** y **Sodimac**, filtra los resultados
por precio, palabras clave y ubicación, los evalúa con un modelo de lenguaje y
avisa por Telegram, correo, Pushover, Pushbullet o ntfy cuando aparece algo que
vale la pena. También puede seguir una página concreta y avisar cuando baja de
precio.

Es un fork de [ai-marketplace-monitor](https://github.com/BoPeng/ai-marketplace-monitor)
de Bo Peng, reescrito en buena parte para esta región y para uso personal.

**Interfaz oficial:** [platform-scrapper-ui](https://github.com/elMaxip/platform-scrapper-ui).
Este repositorio es el monitor y su API; la interfaz web con la que se maneja
vive en ese otro repositorio, junto con el `docker-compose.yml` que levanta los
dos servicios juntos.

## Qué hace

- **Búsquedas por producto**, cada una con sus frases, rango de precio,
  palabras obligatorias y excluidas (por título, por descripción o por ambos),
  vendedores excluidos y ciudades o regiones guardadas.
- **Varias plataformas a la vez**: cada plataforma corre en su propio
  navegador y su propio hilo, y una búsqueda decide en cuáles corre.
- **Evaluación con IA** (OpenAI, Anthropic, DeepSeek, Gemini u Ollama local):
  cada aviso recibe una nota de 1 a 5 y sólo se avisa desde la nota que fijes.
- **Seguimiento de páginas** (`[track.*]`): pega la dirección de un producto y
  el monitor la revisa periódicamente y avisa cuando baja de precio.
- **Revisión de avisos guardados**: los avisos ya vistos se vuelven a abrir cada
  cierto tiempo para detectar bajadas de precio y avisos vendidos o borrados.
- **Recarga de configuración en caliente**: un cambio en el archivo se aplica en
  segundos, incluso a la búsqueda que está corriendo.
- **Sesiones guardadas**: se inicia sesión una vez (o se importan las cookies
  desde el navegador propio) y el monitor reutiliza la sesión.
- **Historial de observaciones** por aviso (precio, cambios, cuándo se vio) que
  la interfaz usa para gráficos, comparaciones y detección de oportunidades.
- **API web** (REST + WebSocket) con la que la interfaz edita la configuración,
  controla el monitor y muestra el registro en vivo.

## Instalación

Requiere Python 3.10 o superior.

```bash
git clone https://github.com/elMaxip/marketplace-scrapper-latam.git
cd marketplace-scrapper-latam

# con uv
uv sync --all-extras
uv run playwright install

# o con pip
pip install -e ".[stealth]"
playwright install
```

El extra `stealth` instala [patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright),
un fork de Playwright que las tiendas con verificación de bots (Lider, Sodimac)
detectan menos. Es opcional: si no está, se usa Playwright normal.

## Uso

```bash
ai-marketplace-monitor          # o el alias corto: aimm
```

Al primer arranque crea `~/.ai-marketplace-monitor/config.toml` vacío y levanta
la API en `http://127.0.0.1:8467`. Desde ahí se agregan las búsquedas con la
[interfaz](https://github.com/elMaxip/platform-scrapper-ui) o editando el
archivo a mano.

Opciones útiles:

| Opción | Qué hace |
| --- | --- |
| `--config <archivo>` | Lee uno o más archivos de configuración además del principal. |
| `--headless` | No muestra la ventana del navegador. |
| `--login` | Abre el navegador para iniciar sesión a mano, sin límite de tiempo, guarda la sesión y sale. |
| `--check <url o id>` | Explica por qué un aviso guardado fue aceptado o rechazado. |
| `--clear-cache <tipo\|sessions\|all>` | Borra la caché indicada (avisos, respuestas de la IA, sesiones…). |
| `--no-webui` | No levanta la API web. |
| `--webui-host`, `--webui-port` | Dónde escucha la API (por defecto `127.0.0.1:8467`). |
| `--verbose` | Muestra mensajes de depuración. |

Fuera de `127.0.0.1` la API pide usuario y contraseña (`FACEBOOK_USERNAME` /
`FACEBOOK_PASSWORD` o la sección `[marketplace.facebook]`).

### Configuración mínima

```toml
[ai.openai]
api_key = "${OPENAI_API_KEY}"
model = "gpt-4o-mini"

[user.yo]
telegram_token = "${TELEGRAM_BOT_TOKEN}"
telegram_chat_id = "123456789"

[item.playstation]
search_phrases = "playstation 5"
min_price = 200000
max_price = 450000
antikeywords = ["control", "solo caja", "cambio"]
rating = 4

[monitor]
search_interval = "30m"
max_search_interval = "1h"
```

Las plataformas no hay que declararlas: todas existen y una búsqueda corre en
todas salvo que diga lo contrario. Cualquier valor puede escribirse como
`"${VARIABLE}"` para leerlo del entorno.

La referencia completa de opciones está en [`docs/README.md`](docs/README.md)
y hay un ejemplo con muchas de ellas en
[`docs/example_config.toml`](docs/example_config.toml).

## Docker

El `Dockerfile` construye una imagen con Chromium, una pantalla virtual (Xvfb)
y noVNC, para poder resolver un CAPTCHA o un inicio de sesión desde el
navegador aunque el monitor corra en un servidor sin pantalla. Se publica en
`ghcr.io/elmaxip/marketplace-scrapper-latam` con cada tag `v*.*.*`.

La forma recomendada de levantarlo es con el `docker-compose.yml` del
[repositorio de la interfaz](https://github.com/elMaxip/platform-scrapper-ui),
que arranca el monitor y la interfaz juntos. Para construirla a mano:

```bash
docker build -t aimm .
docker run -d --name aimm -p 8467:8467 \
  -v "$HOME/.ai-marketplace-monitor:/home/aimm/.ai-marketplace-monitor" \
  aimm
```

Dentro de un contenedor la API tiene que escuchar en `0.0.0.0`; para eso existe
`--webui-open` / `AIMM_WEBUI_OPEN`, que la sirve sin contraseña. Úsalo sólo
detrás de algo que mantenga el puerto privado (la red interna de Compose, una
VPN como Tailscale).

## Documentación

- [`docs/README.md`](docs/README.md) — referencia de todas las opciones de configuración.
- [`docs/webui.md`](docs/webui.md) — qué expone la API y cómo se comporta el monitor detrás de la interfaz.
- [`docs/mercadolibre.md`](docs/mercadolibre.md) — cómo se lee Mercado Libre, su muro de inicio de sesión y cómo importar una sesión.
- [`CHANGELOG.md`](CHANGELOG.md) — cambios por versión.

## Desarrollo

```bash
uv run inv lint      # ruff + black/isort en modo comprobación
uv run inv format    # aplica el formato
uv run inv mypy
uv run inv tests     # pytest con cobertura
uv run pytest tests/test_observations.py -k nombre   # una prueba
```

Sin `uv`, el paquete se resuelve desde `src/`:

```powershell
$env:PYTHONPATH = "src"; python -m pytest tests -q
```

## Aviso

Los términos de uso de estas plataformas restringen la recolección automática de
datos. Este proyecto es de uso personal; quien lo ejecute es responsable de
cumplir los términos de cada sitio y la ley aplicable.

## Licencia

[GNU AGPL v3](LICENSE), la misma del proyecto original.

Créditos al proyecto de origen, [BoPeng/ai-marketplace-monitor](https://github.com/BoPeng/ai-marketplace-monitor),
y a lo que ese a su vez tomó de
[facebook-marketplace-scraper](https://github.com/passivebot/facebook-marketplace-scraper).
