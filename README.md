# Ghost Voice 👻🎙

Panel de sonidos + "di lo que yo digo con voz robótica", enviado a los juegos por un micrófono virtual.

## Puesta en marcha (una sola vez)

1. Instala **VB-Audio Virtual Cable** (gratis): https://vb-audio.com/Cable/
   Descomprime, clic derecho en `VBCABLE_Setup_x64.exe` → *Ejecutar como administrador* → *Install Driver*. Reinicia.
2. Abre la app con **`iniciar.bat`**.
3. Pestaña **⚙ Audio**: en *Salida* elige `CABLE Input (VB-Audio Virtual Cable)`.
4. En el juego / Discord elige como micrófono **`CABLE Output (VB-Audio Virtual Cable)`**.

### Consejos para la máxima calidad
- En el juego/Discord pon el micro en **detección continua o por voz con umbral bajo**, y **desactiva la supresión de ruido / Krisp / Sonar ClearCast** para ese micro: está pensada para voces humanas y puede cortar o distorsionar los sonidos.
- Panel de control de sonido → Grabación → *CABLE Output* → Propiedades → Opciones avanzadas: pon **48000 Hz**. Haz lo mismo con *CABLE Input* en la pestaña Reproducción, para que coincidan.
- Deja los volúmenes al 100 % y súbelos o bájalos por sonido (clic derecho → Volumen). La app usa un limitador en vez de recortar, así que nada "rompe".
- Si un juego se ejecuta como administrador y no responden los atajos, ejecuta también la app como administrador.

## Uso
- **🔊 Sonidos**: añade archivos (o arrástralos), o descarga desde un enlace (MP3 directo, MyInstants, YouTube, SoundCloud…). Clic derecho en un botón para darle un atajo de teclado global.
- **🔒 Bloq Mayús = modo panel**: con Bloq Mayús activado, **F1** (mantener) = hablar y **F2–F9** = sonidos, sin que esas teclas lleguen al juego. Con Bloq Mayús apagado, las F funcionan normal en el juego.
- **🗣 Voz**: mantén pulsada la tecla para hablar (**F1**), habla y suelta. Whisper lo transcribe en local y la voz sintética lo dice por el micrófono virtual.
  - *Modo de idioma*: Inglés→Inglés, Español→Español, Inglés→Español (i→e) o Español→Inglés (e→i). Traducción local, sin internet. También se aplica a lo que escribes.
  - *Escribir y que lo diga*: para cuando no quieres ni hablar. Guarda frases frecuentes con ★.
  - **Windows SAPI** = la voz robótica clásica tipo Loquendo. Si tienes voces Loquendo SAPI5 instaladas, aparecen en la lista.
  - **Windows OneCore** = Pablo, Laura, Helena (y las que añadas en Configuración > Hora e idioma > Voz).
  - **Piper** = ~90 voces neuronales offline muy naturales (se descarga cada voz la primera vez, ~60 MB).
  - **Edge** = ~90 voces neuronales online.
  - **Efectos graciosos**: Pitufo, Hombre rudo, Gigante, Demonio, Robot, Fantasma, Alien, Abuelo tembloroso, Walkie-talkie. Funcionan con cualquier voz.

## Datos
Todo se guarda en `datos/`: `sonidos/`, `modelos/` (Whisper) y `config.json`.
