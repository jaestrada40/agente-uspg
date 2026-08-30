# Seguridad operativa

El servicio conserva telefonos, conversaciones, nombres y citas. En produccion usa
PostgreSQL administrado con cifrado en reposo, conexiones TLS y copias de seguridad
cifradas; limita el acceso a la base de datos al servicio y al personal autorizado.

`DATA_RETENTION_DAYS` define cuantos dias se retienen conversaciones, leads y citas
(90 por defecto). El servicio los elimina al arrancar y cada 24 horas. Para una
solicitud individual de supresion, usa `borrar_datos_personales(telefono)` desde un
proceso administrativo autenticado; no expongas esa operacion como endpoint publico.

En produccion configura siempre `ZERNIO_WEBHOOK_SECRET`, `ENVIRONMENT=production` y
los limites de webhook. El proveedor debe llegar a la app a traves de HTTPS. Si hay
un proxy delante, configura este para preservar de forma confiable la IP de origen
antes de depender del rate limit por IP.
