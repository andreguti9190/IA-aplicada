# VigiWork · Servidor

Aplicación de escritorio con PySide6. Abre cámaras RTSP con OpenCV/FFmpeg, detecta cascos de seguridad de obra y dibuja cajas. Si la cámara pierde la señal, reintenta automáticamente con pausas crecientes. La inferencia usa el módulo DNN de OpenCV y un modelo ONNX local; no requiere PyTorch, Ultralytics, Rust ni Cargo. La ventana principal administra el sistema; **Probar** abre una vista temporal y el cliente recibe el video para su uso normal.

Esta aplicación funciona como **servidor**. Tocá **Probar** para abrir la ventana de prueba, elegir una cámara e iniciar el servicio. Para la misma red, el cliente puede usar la dirección local. Para acceso remoto, tocá **Crear enlace Cloudflare** y compartí la URL temporal y la clave de acceso con el cliente. La URL solo funciona mientras la aplicación y el túnel estén activos, y cambia al crear un túnel nuevo. El cliente muestra el video procesado y los FPS recibidos. Solo el servidor puede agregar o quitar cámaras, y sus URLs y credenciales no se envían al cliente. No compartas la clave con personas que no deban ver el video.

## Inicio en Windows

1. Ejecutá **Instalar.bat** una vez. Crea `.venv`, instala las dependencias, descarga `cloudflared.exe` y prepara el detector ONNX en `models/helmet_detection.onnx`.
2. Para usar el programa, ejecutá **Iniciar.bat**.

Necesitás Python 3 instalado y conexión a Internet durante la instalación.

El enlace usa Cloudflare Quick Tunnels, no requiere cuenta ni dominio, y es temporal. Su URL pública puede cambiar cada vez que se inicia un túnel. Cloudflare documenta esta opción como apta para pruebas y desarrollo: [Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/).

También se puede iniciar desde una terminal:

```bat
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

Al iniciar, la ventana muestra la lista de cámaras guardadas sin abrir video. Tocá **Agregar cámara** para ingresar su nombre y URL RTSP; cada cámara tiene su propia casilla **IA**. Una cámara seleccionada se puede modificar con **Editar**, incluso para cambiar su IP, sus credenciales o la ruta del stream, sin eliminarla y volverla a crear.

Dentro de **Agregar cámara**, el botón **Buscar automáticamente en la red** revisa en segundo plano las direcciones de la subred local `/24` y muestra los equipos que aceptan conexiones TCP en el puerto RTSP 554. Al elegir una IP, VigiWork solicita el usuario, la contraseña y la ruta del stream, y construye automáticamente la URL completa con el formato `rtsp://usuario:contraseña@IP:554/ruta`. La ruta ofrece valores habituales de distintos fabricantes y continúa siendo editable porque no forma parte del descubrimiento del puerto. La búsqueda no prueba contraseñas ni examina direcciones fuera de esa subred.

Al agregar, editar, quitar o cambiar la IA aparece un borrador: **Aplicar cambios** lo guarda y reinicia el servicio, y **Cancelar** lo descarta. **Probar** abre una ventana separada para elegir una cámara y ver su stream; el servidor también envía ese video al cliente. Cerrar la ventana de prueba no interrumpe la transmisión al cliente. La configuración aplicada queda en `cameras.json`. Si la URL contiene credenciales, se guarda localmente en texto plano, así que no compartas ese archivo.

## Alertas por correo

El botón **Configurar alertas por correo** guarda en la configuración local del servidor una cuenta Gmail, una contraseña de aplicación SMTP y el destinatario. Cuando la IA mantiene al menos una detección `SIN CASCO` durante cinco segundos continuos, el servidor envía un correo con la cámara, la fecha, la hora y una captura JPEG. Cada episodio genera un solo aviso; una nueva detección queda sujeta a un enfriamiento de cinco minutos para evitar correos repetidos. La captura y el envío SMTP se ejecutan fuera del hilo de video.

También se puede configurar sin la interfaz mediante variables de entorno:

```text
VIGIWORK_SMTP_USER=cuenta@gmail.com
VIGIWORK_SMTP_PASSWORD=contraseña_de_aplicación
VIGIWORK_ALERT_RECIPIENT=destino@example.com
VIGIWORK_SMTP_HOST=smtp.gmail.com
VIGIWORK_SMTP_PORT=465
VIGIWORK_SMTP_SSL=1
```

Las variables opcionales `VIGIWORK_ALERT_AFTER_SECONDS` y `VIGIWORK_ALERT_COOLDOWN_SECONDS` permiten cambiar los valores predeterminados de 5 y 300 segundos. No guardes contraseñas reales en el repositorio. Si la IA está apagada para una cámara, esa cámara no genera alertas.

## GPU y detección

El detector marca los cascos de seguridad en verde y las detecciones `NO-Hardhat` en rojo. Intenta usar CUDA cuando OpenCV y el controlador NVIDIA lo permiten; en los demás equipos usa CPU, que es la opción más compatible. OpenCL queda desactivado por defecto porque algunos controladores Intel fallan al compilar los kernels de OpenCV. Para probarlo explícitamente, definí `VIGIWORK_ENABLE_OPENCL=1` antes de iniciar la aplicación; si falla durante la inferencia, el detector cambia a CPU automáticamente. La ventana muestra el backend activo, los FPS y ambos conteos.

Los mensajes H.264 como `error while decoding MB` indican cuadros dañados o incompletos recibidos desde la cámara. VigiWork fuerza RTSP sobre TCP y vuelve a conectarse cuando la lectura se corta, pero no puede reparar un flujo corrupto. Si ocurre con frecuencia, usá el substream de la cámara, reducí resolución o bitrate, aumentá el intervalo entre cuadros clave y revisá el cableado, Wi-Fi y firmware de la cámara.

La captura y la inferencia funcionan en paralelo. El servidor siempre procesa el cuadro más reciente y descarta trabajo de IA atrasado, por lo que una CPU lenta puede reducir la frecuencia de actualización de las cajas sin frenar el video. La IA comienza con un objetivo de 6 cuadros por segundo —configurable con `VIGIWORK_AI_MAX_FPS`— y después el perfil elegido lo recalibra con mediciones reales. Para el cliente, los cuadros se limitan por defecto a 960 píxeles de ancho y JPEG 65; la IA sigue analizando la imagen original. Se puede ajustar con `VIGIWORK_STREAM_MAX_WIDTH` (480–1920) y `VIGIWORK_JPEG_QUALITY` (40–90). Para un equipo potente y una red rápida se puede usar ancho `1280` y calidad `75`; en un N305 conviene mantener los valores predeterminados y usar Auto antes de probar niveles manuales.

## Ajuste automático de rendimiento

El servidor no confía en los FPS declarados por RTSP: mide continuamente la llegada real de cuadros durante una ventana móvil de cinco segundos. Captura, inferencia y compresión MJPEG se ejecutan de forma independiente, por lo que una cámara de 15, 30 o 60 FPS no queda limitada por una suposición fija ni por una inferencia lenta. La pantalla informa por separado **Cámara**, **Transmisión** e **IA**.

El perfil **Auto** reserva margen para la interfaz y la red y recalcula la frecuencia de IA según la latencia medida. Los niveles manuales 1–9 utilizan progresivamente del 60% al 100% de la capacidad de inferencia observada, sin intentar superar los FPS recibidos de la cámara. El perfil puede cambiarse mientras el video está activo y queda guardado para el siguiente inicio. En esta primera versión los niveles controlan la frecuencia de análisis; futuras versiones pueden usar el margen restante para recortes y análisis multiescala.

El modelo base es YOLOv8n, afinado para EPP de construcción. OpenCV corre la inferencia desde ONNX sin instalar PyTorch. La clase `NO-Hardhat` puede fallar más que la clase de casco; el tamaño, la distancia, las oclusiones y la iluminación también afectan lo que detecta. El modelo no publica métricas de validación propias, así que revisá los resultados con las cámaras del lugar antes de usarlo como apoyo operativo.

Modelo: [Construction Site PPE Detector](https://huggingface.co/mancalazure/construction-ppe-safety), clases `Hardhat` y `NO-Hardhat`, licencia CC BY 4.0. La descarga está fijada a una revisión y se verifica con SHA-256. Los cuadros se procesan localmente y no se guardan.
