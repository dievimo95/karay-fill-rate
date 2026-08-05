# Karay Fill Rate 2.2

Aplicación Streamlit para cargar varios pedidos y facturas PDF, consolidarlos por orden de compra y código de producto, calcular el Fill Rate y la venta dejada de facturar, y conservar cada procesamiento en SQLite.

## Qué incluye

- Carga múltiple de pedidos y facturas PDF.
- Suma de varias facturas asociadas a una misma OC/producto.
- Fill Rate por unidades, sin inflarlo por sobreentregas.
- Venta potencial, venta facturada y venta dejada de facturar.
- Guardado automático y sin duplicados en `fillrate.db`.
- Histórico con filtros, detalle y descarga CSV.
- Inicio de sesión configurable para Diego y su asistente.
- Compatibilidad con PostgreSQL para conservar el Histórico en Internet.
- Preparada para publicar en Streamlit Community Cloud.

## Instalación en Mac

1. Descomprime la carpeta y ábrela en Finder.
2. Abre Terminal, escribe `cd ` (incluye el espacio) y arrastra esta carpeta a Terminal. Presiona Enter.
3. Instala las dependencias:

   ```bash
   python3 -m pip install -r requirements.txt
   ```

4. Inicia la aplicación:

   ```bash
   python3 -m streamlit run app.py
   ```

La aplicación se abrirá en el navegador. Para detenerla, vuelve a Terminal y presiona `Control + C`.

## Cómo reemplazar la versión actual

1. Detén la versión anterior con `Control + C`.
2. Haz una copia de seguridad de su carpeta completa.
3. Copia `app.py`, `requirements.txt`, `README.md` y `fillrate.db` de esta entrega a la carpeta actual.
4. Cuando macOS pregunte, selecciona **Reemplazar** para los tres archivos de texto. No reemplaces una base histórica que ya estés usando; conserva ese `fillrate.db`.
5. Ejecuta de nuevo los comandos de instalación e inicio indicados arriba.

## Copias de seguridad

Todo el histórico está en el archivo `fillrate.db`. Con la aplicación detenida, copia ese archivo periódicamente a una ubicación segura. No lo abras ni reemplaces mientras la aplicación esté procesando documentos.

## Publicación con enlace

La publicación necesita dos servicios:

1. Un repositorio privado en GitHub con los archivos del proyecto.
2. Una base PostgreSQL en Internet. El proveedor entregará una dirección que comienza con `postgresql://`.

Después:

1. Entra a `https://share.streamlit.io` con GitHub.
2. Selecciona **Create app** y el repositorio de Karay Fill Rate.
3. Indica `app.py` como archivo principal.
4. Abre **Advanced settings > Secrets**.
5. Copia el contenido de `.streamlit/secrets.toml.example`, reemplaza la dirección de la base y define las dos contraseñas.
6. Pulsa **Deploy** y comparte el enlace terminado en `streamlit.app`.

No subas a GitHub un archivo llamado `.streamlit/secrets.toml`: las contraseñas y la dirección real de la base deben guardarse exclusivamente en la sección **Secrets** de Streamlit.

Sin configuración de Secrets, la aplicación continúa funcionando localmente con SQLite y sin pantalla de acceso.

## Formato de los PDFs

El lector intenta reconocer tablas con columnas equivalentes a código/EAN, descripción, cantidad, precio unitario y OC. También incluye un lector alternativo para pedidos de Corporación Favorita y facturas electrónicas en las que código, descripción y cantidad aparecen como texto.

En los pedidos de Corporación Favorita, la cantidad en cajas se convierte automáticamente a unidades mediante la columna `UC`. Las OCs separadas que aparezcan dentro de un mismo PDF se conservan individualmente.

Si un proveedor cambia el diseño del PDF, la aplicación mostrará el nombre del archivo que no pudo interpretar. Los PDFs escaneados como imagen requieren OCR y no se procesan en esta versión.

## Cálculos

- `Fill Rate = unidades facturadas / unidades pedidas × 100`
- `Pendientes = máximo(pedidas − facturadas, 0)`
- `Venta dejada de facturar = pendientes × precio unitario del pedido`

Las sobreentregas no elevan el Fill Rate por encima de 100%.
