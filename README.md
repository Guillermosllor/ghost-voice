# Ghost Voice 👻🎙

Para quien juega en línea y prefiere no hablar con su propia voz. Ghost Voice envía a tus juegos y a Discord, a través de un micrófono virtual:

- **Un panel de sonidos**: memes, música y efectos con un atajo de teclado.
- **Una voz sintética que repite lo que dices.** Hablas (o escribes) y la escuchan con voz de robot, de Loquendo, de Stephen Hawking, neuronal o clonada, y también traducida inglés ↔ español.

Todo se procesa en tu PC, sin enviar tu voz a ningún sitio. Lo más importante es la calidad: audio de 32 bits, remuestreo de alta calidad, volumen igualado (LUFS) y un limitador que nunca satura.

## Requisitos

- Windows 10 u 11.
- [Python 3.14](https://www.python.org/downloads/).
- [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) (gratis): es el micrófono virtual.
- Recomendada una gráfica NVIDIA: el reconocimiento de voz va unas 10 veces más rápido. Sin ella también funciona, con la CPU.

## Instalación

1. **VB-Cable**: descomprímelo, haz clic derecho en `VBCABLE_Setup_x64.exe` → *Ejecutar como administrador* → *Install Driver*. Reinicia el PC.
2. **La app**: en la carpeta del proyecto, ejecuta:
   ```
   py -3.14 -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   ```
3. Ábrela con **`iniciar.bat`**.
4. En la pestaña **⚙ Audio**, en *Micrófono virtual*, elige `CABLE Input (VB-Audio Virtual Cable)`.
5. En el juego o en Discord, elige como micrófono **`CABLE Output (VB-Audio Virtual Cable)`**.

La primera vez que uses cada función, la app descarga sola lo que necesita: el modelo de reconocimiento de voz (Whisper), el traductor, la voz Kokoro (unos 350 MB) y cada voz de Piper.

## Cómo se usa

### 🔒 Bloq Mayús = modo panel

Con **Bloq Mayús activado**:

- **F1** (mantener) = hablar.
- **F2 a F9** = sonidos.

Esas teclas **no llegan al juego**. Al apagar Bloq Mayús, las teclas F vuelven a funcionar normal en el juego y se corta todo lo que estuviera sonando. Un pitido que sube o baja te indica en qué modo estás.

### 🔊 Sonidos

- **Añadir sonidos**: añade archivos o arrástralos a la ventana (MP3, WAV, FLAC, OGG, M4A…, y también vídeos como MP4, MKV o MPEG, de los que se extrae el audio).
- **Descargar de internet**: desde un enlace directo, YouTube, SoundCloud, MyInstants… Para vídeos con **restricción de edad**, la app usa tu sesión de YouTube del navegador que elijas. Opera, Chrome, Edge y Brave tienen que estar **cerrados del todo** mientras descarga.
- **✂ (esquina del botón)**: recorta el sonido con unos tiradores sobre la forma de onda.
- **Clic derecho en un botón**: renombrar, volumen, atajo de teclado o eliminar.

### 🗣 Voz

Mantén **F1**, habla y suelta. Whisper transcribe lo que dices y la voz elegida lo dice por el micrófono virtual, en un segundo y medio aproximadamente.

- **Modo de idioma**: inglés → inglés, español → español, inglés → español o español → inglés. La traducción es local, sin internet.
- **Escribir y que lo diga**: para cuando no quieres ni hablar. Las frases frecuentes se guardan con ★.
- **Guardar como sonido**: cualquier frase, con su voz y su efecto, se puede convertir en un botón del panel.
- **⭐ Mis voces**: acceso rápido a tus mezclas y voces clonadas.
- **Efectos**: Pitufo, Hombre rudo, Gigante, Demonio, Robot, Fantasma, Alien, Abuelo tembloroso y Walkie-talkie. Funcionan con cualquier voz.

**Motores de voz:**

| Motor | Qué es |
|---|---|
| **Kokoro** | Neuronal, offline y muy natural. Puedes **mezclar varias voces** para crear la tuya (🎛). |
| **🧬 Voces clonadas** | Habla con la voz de un audio de 10 a 20 s. Necesita instalarse aparte: ver [clonador/README.md](clonador/README.md). |
| **eSpeak NG** | Robots de verdad y una voz al estilo Stephen Hawking (síntesis Klatt). |
| **Windows SAPI** | La robótica clásica tipo Loquendo. Si tienes voces Loquendo SAPI5, aparecen aquí. |
| **Windows OneCore** | Pablo, Laura, Helena… y las que añadas en Configuración → Hora e idioma → Voz. |
| **Piper** | Unas 90 voces neuronales offline; cada una se descarga la primera vez. |
| **Microsoft Edge** | Voces neuronales online. |

## Para que en Discord se te oiga bien

Discord está pensado para voces humanas y **corta los sonidos y las voces de robot**. Para evitarlo:

1. En Discord → Voz y vídeo → **Modo de entrada: Pulsar para hablar**, y asígnale una tecla que no uses (por ejemplo **Bloq Despl**).
2. En Ghost Voice → ⚙ Audio → **Pulsar para hablar automático**: pon la misma tecla. La app la mantiene pulsada mientras suena algo. Si a tus amigos les llega cortado el principio, sube el margen.
3. En Discord, **desactiva la supresión de ruido (Krisp), la cancelación de eco y el control automático de ganancia**.
4. Si tienes permisos en el servidor, sube la **tasa de bits** del canal de voz al máximo.

## Consejos de calidad

- Deja los volúmenes del micrófono virtual y de la voz al **100 %**. La app ya iguala el volumen de cada sonido; por encima del 100 % el limitador tiene que comprimir el sonido.
- Panel de control de sonido → *CABLE Output* (Grabación) y *CABLE Input* (Reproducción) → Propiedades → Opciones avanzadas: pon los dos a **48000 Hz**.
- Si un juego se ejecuta como administrador y no responden las teclas, abre también Ghost Voice como administrador.

## Datos

Todo lo tuyo se guarda en `datos/`, que no se sube a git:

- `sonidos/`
- `voces_clonadas/`
- `modelos/`
- `config.json`
- `registro.log`: lo consultas si algo falla.
