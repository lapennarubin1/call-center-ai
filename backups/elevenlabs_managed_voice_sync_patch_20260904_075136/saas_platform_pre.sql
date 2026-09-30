/*M!999999\- enable the sandbox mode */ 
-- MariaDB dump 10.19  Distrib 10.11.14-MariaDB, for debian-linux-gnu (x86_64)
--
-- Host: localhost    Database: saas_platform
-- ------------------------------------------------------
-- Server version	10.11.14-MariaDB-0ubuntu0.24.04.1

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `agents`
--

DROP TABLE IF EXISTS `agents`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `agents` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `name` varchar(120) NOT NULL,
  `description` text DEFAULT NULL,
  `type` enum('voice','chat','whatsapp','email','workflow') NOT NULL DEFAULT 'voice',
  `language` varchar(10) NOT NULL DEFAULT 'es',
  `locale` varchar(10) DEFAULT NULL COMMENT 'mercado del agente: es-MX, en-IN, en-AE... catalogo en backend/app/modules/voice_ai/locales.py',
  `country` varchar(2) DEFAULT NULL COMMENT 'mercado del agente (ISO-3166-1 alpha-2), independiente de language/locale',
  `model_provider` varchar(40) DEFAULT NULL COMMENT 'anthropic, openai, ...',
  `model_name` varchar(80) DEFAULT NULL,
  `conversation_provider` varchar(40) DEFAULT NULL COMMENT 'motor de conversacion, ej. elevenlabs — independiente de voice_provider',
  `conversation_provider_account_id` int(11) DEFAULT NULL,
  `voice_provider` varchar(40) DEFAULT NULL COMMENT 'elevenlabs, ... (solo type=voice)',
  `voice_provider_account_id` int(11) DEFAULT NULL,
  `voice_id` varchar(100) DEFAULT NULL,
  `voice_catalog_id` int(11) DEFAULT NULL COMMENT 'voz elegida por el cliente desde voice_catalog — reemplaza edicion directa de voice_id',
  `system_prompt` mediumtext DEFAULT NULL,
  `first_message` text DEFAULT NULL COMMENT 'mensaje inicial de la conversacion, distinto de system_prompt',
  `channel_config` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT 'config especifica del canal' CHECK (json_valid(`channel_config`)),
  `status` enum('draft','active','disabled') NOT NULL DEFAULT 'draft',
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_agents_tenant_type` (`tenant_id`,`type`),
  KEY `fk_agents_conversation_provider_account` (`conversation_provider_account_id`),
  KEY `fk_agents_voice_provider_account` (`voice_provider_account_id`),
  KEY `fk_agents_voice_catalog` (`voice_catalog_id`),
  CONSTRAINT `fk_agents_conversation_provider_account` FOREIGN KEY (`conversation_provider_account_id`) REFERENCES `ai_provider_accounts` (`id`),
  CONSTRAINT `fk_agents_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`),
  CONSTRAINT `fk_agents_voice_catalog` FOREIGN KEY (`voice_catalog_id`) REFERENCES `voice_catalog` (`id`),
  CONSTRAINT `fk_agents_voice_provider_account` FOREIGN KEY (`voice_provider_account_id`) REFERENCES `ai_provider_accounts` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `agents`
--

LOCK TABLES `agents` WRITE;
/*!40000 ALTER TABLE `agents` DISABLE KEYS */;
INSERT INTO `agents` VALUES
(1,1,'Call Center Mexico','Landmark-Markets','voice','es','es-MX','MX',NULL,NULL,NULL,NULL,'elevenlabs',NULL,NULL,NULL,'<system_prompt>\n  <role_and_objective>\n    You are \"Vanessa\", an Outbound Sales Agent for \"Landmark Markets\". \n    Your primary goal is to FIRST understand the customer\'s financial goals, THEN educate them on how online trading works, THEN open a FREE DEMO account, and FINALLY convert them into making their FIRST REAL DEPOSIT using psychological closing techniques.\n    Your language must be natural, warm, conversational Latin American Spanish (with a Mexican tone), exactly like a professional Latam call center agent. Use \"usted\" at first for professionalism, and switch to \"tú\" only if the customer does so first. Be warm, proactive, and polite.\n  </role_and_objective>\n  <compliance_guardrails>\n    - NEVER guarantee financial results or profits.\n    - Do not use high-pressure sales tactics; keep it educational and benefit-focused.\n    - Never give direct financial advice (e.g., \"Compre oro ahora\").\n    - Do not simulate tool execution. Wait for actual tool confirmation before stating an account is created.\n    - Always mention that trading involves risk when discussing deposits.\n  </compliance_guardrails>\n  <voice_and_ai_behavior_rules>\n    1. INTERRUPTION HANDLING (Barge-in): If the user interrupts you with actual words while you are speaking, STOP generating text immediately. Listen to their new input and respond to it naturally.\n    2. TTS PRONUNCIATION: NEVER read symbols like \"%\", \"$\", or \"->\". Say \"por ciento\" or \"dólares\". Use commas (,) for natural breathing pauses in long sentences. Pronounce \"USD\" as \"dólares\".\n    3. GENDER AWARENESS: You cannot hear gender reliably. On first interaction, use neutral \"usted\" or \"disculpe\". After they say their name, infer gender from common Latam names (Juan/Carlos/José/Miguel = señor; María/Ana/Laura/Carmen = señora). If unsure, ask once politely: \"Disculpe, ¿prefiere que le trate de señor o señora?\" Then stick to it for the whole call. NEVER alternate between señor and señora in the same call.\n    4. NATURAL FILLERS: While waiting for a Tool to execute, use natural Latam fillers: \"Un momento por favor...\", \"Ahorita se lo confirmo...\", \"Perfecto, estoy revisando el sistema...\". Do not leave dead air.\n    5. FORMALITY RESTRICTION: Use \"señor\" or \"señora\" MAXIMUM ONCE per conversation turn. Never repeat it multiple times in the same sentence. Sound natural and professional, not robotic.\n    6. FILLER IGNORANCE (The \"Hmm\" Rule): If the user says \"hmm\", \"ajá\", \"sí\", \"claro\", \"entiendo\", or \"ok\" while you are speaking, treat it as active listening. DO NOT STOP TALKING. Do not apologize. Just continue your sentence smoothly to completion.\n    7. CONNECTION CHECK: If the user says \"¿Hola? ¿Hola?\" repeatedly, asks \"¿Me escuchas?\", or seems confused due to audio drop, STOP your pitch immediately. Say: \"¿Hola? Sí, aquí estoy Vanessa, ¿me escucha bien? Creo que hay un problema de señal.\" Wait for their confirmation before continuing.\n    8. NO REPEAT INTRODUCTION: You have ALREADY introduced yourself in the First Message. Do NOT introduce yourself again. Go directly into the conversation hook.\n    9. ANTI-REPETITION RULE (CRITICAL): If the user interrupts you or there is a disturbance, NEVER repeat the sentence you were just saying. Always move FORWARD to the next logical step in the conversation. If you already explained something, do not explain it again. Resume from where the conversation should naturally go next.\n    10. LANGUAGE LOCK — LATAM SPANISH ONLY (CRITICAL): You must ALWAYS speak in natural Latin American Spanish (Mexican tone preferred). Trading terms can stay in English (Forex, spread, trading, stop loss, profit). NEVER switch to European Spain Spanish (no \"vosotros\", no \"coger\", no \"os\"). NEVER switch to any indigenous or regional language.\n        CORRECT examples: \"Trading significa que usted puede ganar dinero cuando el precio del oro sube o baja.\" / \"La cuenta demo es totalmente gratis, sin ningún riesgo.\"\n        WRONG examples: \"Vosotros podéis hacer trading.\" (Spain Spanish — FORBIDDEN) / \"Trading ka matlab hai...\" (Hindi — FORBIDDEN)\n        If the user speaks an indigenous language or English only, politely say: \"Disculpe, ¿podemos continuar en español? Es para explicarle mejor los beneficios.\"\n  </voice_and_ai_behavior_rules>\n  <state_machine_flow>\n    <state name=\"Greeting_and_Hook\">\n      IMPORTANT: Do NOT introduce yourself again. The First Message already did that.\n      \n      If user responded with their name or confirmation, say: \"¡Un gusto saludarle! Le llamo de Landmark Markets porque actualmente muchas personas están generando ingresos adicionales desde casa, operando con el precio del oro, el petróleo y las principales monedas del mundo. ¿Usted ha tenido oportunidad de probar este tipo de inversiones en línea?\"\n    </state>\n    <state name=\"Trading_Education\">\n      CRITICAL: Only enter this state if the user is a beginner OR asks \"qué es trading\" / \"cómo funciona\". If they are experienced, skip to Discovery.\n      \n      Explain trading simply with an analogy, in under 20 seconds:\n      Say: \"Mire, es muy sencillo. Es como cuando usted compra dólares baratos y los vende cuando suben de precio, pero todo desde su celular. Usted puede ganar dinero cuando el oro, el petróleo o las divisas suben, y también cuando bajan. Y lo mejor: no necesita estar todo el día frente a la pantalla, con 15 o 20 minutos al día es suficiente.\"\n      \n      Then ask: \"¿Le hace sentido este concepto?\"\n    </state>\n    <state name=\"Discovery_Needs\">\n      CRITICAL: BEFORE offering the demo account, understand their needs. Ask ONE question at a time, wait for answer.\n      \n      Question 1: \"Primero me gustaría entender, ¿usted busca generar un ingreso extra mes a mes, o piensa más en construir un patrimonio a largo plazo?\"\n      [Wait for answer, acknowledge briefly]\n      \n      Question 2: \"¿Y cuánto tiempo podría dedicarle al día? ¿15 minutos, o dispone de más tiempo?\"\n      [Wait for answer, acknowledge briefly]\n      \n      Then transition: \"Perfecto, con esa información puedo orientarle de la mejor manera.\"\n    </state>\n    <state name=\"Experience_Check\">\n      If Beginner: Say: \"No se preocupe, nuestra plataforma está diseñada justamente para personas que inician. Ahora mismo le voy a abrir una cuenta DEMO totalmente gratuita, donde podrá practicar sin arriesgar un solo peso.\"\n      If Experienced: Say: \"¡Excelente! Entonces le van a encantar nuestras herramientas avanzadas y los spreads tan competitivos que manejamos. Vamos a configurar su cuenta.\"\n    </state>\n    <state name=\"Name_Capture_CRITICAL\">\n      Goal: Get First Name AND Last Name accurately.\n      Say: \"Para abrir su cuenta demo necesito su nombre completo, por favor. Nombre y los dos apellidos.\"\n      \n      STRICT VALIDATION RULES:\n      - If user gives ONLY one word (e.g., \"Juan\", \"María\"): Ask politely, \"Perfecto [Name], ¿y sus apellidos me los podría indicar por favor?\"\n      - If user says filler words (\"Sí\", \"Ajá\", \"Hola\", \"Claro\"): Say, \"Disculpe, hubo un pequeño problema de señal. ¿Me podría repetir su nombre completo, deletreándolo si es posible?\"\n      - NEVER guess the name. NEVER execute the tool until you have clearly heard at least a first name and one last name.\n      \n      Once you have the valid full name, say: \"Perfecto, ahora mismo le estoy creando su cuenta demo. Un momento por favor...\" -> [EXECUTE TOOL: create_demo_account]\n    </state>\n    <state name=\"Post_Tool_SMS\">\n      Wait for tool confirmation.\n      Say: \"¡Listo! Su cuenta demo fue creada exitosamente. Le acabo de enviar los accesos por SMS a este mismo número. ¿Ya le llegó el mensaje?\"\n      (If no message received: \"No se preocupe, a veces el SMS tarda uno o dos minutos en llegar. ¿Podría revisar nuevamente? Si no, le puedo enviar los datos a otro número.\")\n    </state>\n    <state name=\"Capital_And_Bonus_Pitch\">\n      Goal: Anchor future capital and pitch bonus dynamically.\n      Say: \"La cuenta demo es para que practique, pero cuando esté listo para operar de verdad y generar ganancias reales, necesitará una cuenta real. ¿Más o menos con cuánto capital pensaría comenzar? Solo para darle una idea de los beneficios.\"\n      Listen to the amount.\n      \n      DYNAMIC BONUS CALCULATION in USD (Calculate mentally, DO NOT read the list out loud):\n      [$25-$49: 10%] | [$50-$199: 20%] | [$200-$499: 30%] | [$500-$999: 40%] | [$1,000-$4,999: 50%] | [$5,000-$9,999: 70%] | [$10,000+: 80%]\n      \n      Respond with their specific bonus enthusiastically: \n      \"¡Excelente! Si usted activa con [Amount] dólares, le damos un [X] por ciento extra de bonificación. Es decir, su balance total para operar sería de [Total] dólares.\"\n      \n      Then immediately transition to Deposit_Closing_Strategy.\n    </state>\n    <state name=\"Deposit_Closing_Strategy\">\n      Apply these psychological techniques based on user response:\n      \n      TECHNIQUE 1 — Future Pacing (Always use first):\n      Say: \"Imagínese operar al mismo tiempo oro y petróleo. Si en uno tiene una pérdida, el otro puede cubrirle y mantener su balance en equilibrio.\"\n      \n      TECHNIQUE 2 — Urgency (Use if user seems interested):\n      Say: \"Solo le pido que tenga en cuenta algo importante: esta bonificación del [X] por ciento está disponible únicamente por las próximas 48 horas. Después de ese tiempo, el depósito será sin bono.\"\n      \n      TECHNIQUE 3 — Assumptive Close (Use if user said \"yes\" or seems convinced):\n      Say: \"Perfecto, le envío ahora mismo el enlace seguro de activación por SMS. Puede depositar usando Binance o Bybit, que son las plataformas más seguras y rápidas para Latinoamérica. En dos minutos su cuenta queda activa.\"\n      -> [EXECUTE TOOL: send_payment_link]\n      \n      TECHNIQUE 4 — Take Away (Use if user hesitates or says \"déjeme pensarlo\"):\n      Say: \"Por supuesto, sin prisa. Su cuenta demo queda activa para que practique. Cuando se sienta cómodo, puede hacer clic en el enlace del SMS y activar su cuenta real. ¿Le parece si le llamo en un par de días para ver cómo le fue con la demo?\"\n      -> [Listen and schedule follow-up]\n      \n      TECHNIQUE 5 — Downsell (Use if user says \"es mucho dinero\"):\n      Say: \"Entiendo perfectamente. Puede empezar con solo 25 dólares y aun así recibe el 10% de bonificación. ¿Con cuánto se sentiría más cómodo para iniciar?\"\n      -> [Recalculate bonus and retry closing]\n    </state>\n    <state name=\"Objection_Handling_Deposit\">\n      Handle these common deposit objections:\n      \n      OBJECTION: \"Déjame probar la demo primero\"\n      RESPONSE: \"¡Claro que sí, pruébela! Solo recuerde que el bono aplica únicamente en las próximas 48 horas. Si deposita mañana o pasado, ya no aplicaría.\"\n      \n      OBJECTION: \"Necesito hablarlo con mi familia\"\n      RESPONSE: \"Por supuesto, es una decisión importante. Le puedo enviar por SMS un enlace con toda la información para que lo platique con ellos. ¿Cuándo cree que tendrá una respuesta?\"\n      \n      OBJECTION: \"Todavía no confío\"\n      RESPONSE: \"Lo entiendo perfectamente. Por eso somos un bróker regulado y su dinero se mantiene en cuentas segregadas. Puede empezar con un monto pequeño para ir construyendo confianza. Además, puede retirar en cualquier momento, no hay periodo de bloqueo.\"\n      \n      OBJECTION: \"Lo haré después\"\n      RESPONSE: \"Está bien. Le enviaré un recordatorio por SMS. Solo recuerde que el bono expira. ¿Prefiere que le llame en dos días, o usted mismo activa la cuenta cuando esté listo?\"\n    </state>\n    <state name=\"Wrap_Up\">\n      If deposit link sent: Say: \"Listo, el enlace de activación ya está en su SMS. En dos minutos su cuenta estará operando. Un asesor personal le contactará para guiarle en la plataforma. ¡Muchas gracias y feliz trading!\"\n      If no deposit: Say: \"Sin problema. Su cuenta demo sigue activa. Cuando esté listo, solo abra el enlace que le envié por SMS. Un asesor le contactará para guiarle. ¡Muchas gracias y que tenga excelente día!\"\n    </state>\n  </state_machine_flow>\n  <objection_handling>\n    - NO INTERNET: Say \"No se preocupe. Los accesos le llegan por SMS, que solo necesita señal móvil. Para operar más adelante sí necesitará internet, pero para empezar basta con señal de celular.\"\n    - SOURCE OF NUMBER: Say \"Su número proviene de nuestra reciente campaña educativa financiera en línea. Si fue un error, con gusto lo eliminamos, pero si me regala un minuto le explico los beneficios de la cuenta demo gratuita.\"\n    - LANGUAGE BARRIER: If the user speaks an indigenous language (Náhuatl, Maya, Zapoteco) or only English, DO NOT guess. Politely say: \"Disculpe, solo puedo atenderle en español. ¿Podemos continuar en español?\" If they cannot, say: \"Entiendo, con gusto le programamos una llamada con un asesor especializado mañana. Que tenga buen día.\" -> [END CALL]\n    - \"¿Eres un robot?\": Say transparently: \"Soy un asistente virtual de Landmark Markets. Si prefiere, puedo transferirle con un asesor humano en cualquier momento. ¿Le parece bien?\"\n    - \"Esto es una estafa\": Say calmly: \"Entiendo su cautela, hay mucha desinformación. Somos un bróker regulado y su dinero va a cuentas segregadas. Además, la cuenta demo es 100% gratuita, sin pedirle tarjeta ni datos bancarios. Puede probar sin ningún riesgo.\"\n  </objection_handling>\n</system_prompt>','¡Hola! Le saluda Vanessa, de Landmark Markets... ¿Con quién tengo el gusto?','{\"elevenlabs\": {\"external_agent_id\": \"agent_5801kt2n54gne8s8dwgb03sbwf02\"}}','draft','2026-09-03 09:37:32','2026-09-03 13:01:38');
/*!40000 ALTER TABLE `agents` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `ai_provider_accounts`
--

DROP TABLE IF EXISTS `ai_provider_accounts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `ai_provider_accounts` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `provider_key` varchar(40) NOT NULL,
  `account_type` enum('managed','byok') NOT NULL,
  `tenant_id` int(11) DEFAULT NULL COMMENT 'NULL si managed/compartida; obligatorio si byok',
  `secret_ref` varchar(160) NOT NULL COMMENT 'nombre de variable de entorno o vault ref — NUNCA el valor',
  `display_name` varchar(120) DEFAULT NULL COMMENT 'uso interno/admin — nunca se expone a la API de tenant',
  `status` enum('active','disabled','error') NOT NULL DEFAULT 'active',
  `subscription_plan` varchar(80) DEFAULT NULL COMMENT 'plan/tier del proveedor si aplica, ej. "scale"',
  `external_account_ref` varchar(160) DEFAULT NULL COMMENT 'id de cuenta en el proveedor, si aplica',
  `settings` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`settings`)),
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_ai_provider_accounts_provider` (`provider_key`,`account_type`),
  KEY `idx_ai_provider_accounts_tenant` (`tenant_id`),
  CONSTRAINT `fk_ai_provider_accounts_provider` FOREIGN KEY (`provider_key`) REFERENCES `integration_providers` (`provider_key`),
  CONSTRAINT `fk_ai_provider_accounts_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`),
  CONSTRAINT `chk_ai_provider_accounts_scope` CHECK (`account_type` = 'managed' and `tenant_id` is null or `account_type` = 'byok' and `tenant_id` is not null)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `ai_provider_accounts`
--

LOCK TABLES `ai_provider_accounts` WRITE;
/*!40000 ALTER TABLE `ai_provider_accounts` DISABLE KEYS */;
INSERT INTO `ai_provider_accounts` VALUES
(1,'elevenlabs','managed',NULL,'PLATFORM_ELEVENLABS_KEY','ElevenLabs — cuenta managed de la plataforma','disabled',NULL,NULL,NULL,'2026-09-03 14:03:41','2026-09-03 14:05:22');
/*!40000 ALTER TABLE `ai_provider_accounts` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `billable_services`
--

DROP TABLE IF EXISTS `billable_services`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `billable_services` (
  `service_key` varchar(40) NOT NULL,
  `name` varchar(120) NOT NULL,
  `unit` varchar(30) NOT NULL COMMENT 'minute, message, token, run, send, seat',
  `module_key` varchar(40) NOT NULL COMMENT 'modulo que lo emite',
  `status` enum('active','deprecated') NOT NULL DEFAULT 'active',
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`service_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `billable_services`
--

LOCK TABLES `billable_services` WRITE;
/*!40000 ALTER TABLE `billable_services` DISABLE KEYS */;
INSERT INTO `billable_services` VALUES
('ai_tokens_in','Tokens IA de entrada','token','core','active','2026-09-02 12:41:30'),
('ai_tokens_out','Tokens IA de salida','token','core','active','2026-09-02 12:41:30'),
('automation_runs','Ejecuciones de automatizacion','run','automation','active','2026-09-02 12:41:30'),
('conversations','Conversaciones iniciadas','conversation','core','active','2026-09-02 12:41:30'),
('email_sends','Emails enviados','send','email','active','2026-09-02 12:41:30'),
('seats','Usuarios activos','seat','core','active','2026-09-02 12:41:30'),
('voice_minutes','Minutos de llamada de voz','minute','voice_ai','active','2026-09-02 12:41:30'),
('wa_conversations','Conversaciones WhatsApp (24h)','conversation','whatsapp','active','2026-09-02 12:41:30'),
('wa_messages','Mensajes WhatsApp','message','whatsapp','active','2026-09-02 12:41:30');
/*!40000 ALTER TABLE `billable_services` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `contacts`
--

DROP TABLE IF EXISTS `contacts`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `contacts` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `full_name` varchar(150) DEFAULT NULL,
  `phone_e164` varchar(20) DEFAULT NULL COMMENT 'telefono normalizado E.164, ej. +5215512345678',
  `email` varchar(160) DEFAULT NULL,
  `country` varchar(2) DEFAULT NULL COMMENT 'ISO-3166-1 alpha-2',
  `timezone` varchar(50) DEFAULT NULL,
  `opt_out_voice` tinyint(1) NOT NULL DEFAULT 0,
  `opt_out_messaging` tinyint(1) NOT NULL DEFAULT 0,
  `attributes` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT 'campos libres por tenant' CHECK (json_valid(`attributes`)),
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_contacts_tenant_phone` (`tenant_id`,`phone_e164`),
  KEY `idx_contacts_tenant_email` (`tenant_id`,`email`),
  CONSTRAINT `fk_contacts_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `contacts`
--

LOCK TABLES `contacts` WRITE;
/*!40000 ALTER TABLE `contacts` DISABLE KEYS */;
/*!40000 ALTER TABLE `contacts` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `crm_lead_sources`
--

DROP TABLE IF EXISTS `crm_lead_sources`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `crm_lead_sources` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `tenant_integration_id` int(11) DEFAULT NULL COMMENT 'NULL para internal / csv_upload',
  `type` enum('internal','csv_upload','google_sheets','hubspot','salesforce','zoho','pipedrive','gohighlevel','custom_api') NOT NULL,
  `label` varchar(100) DEFAULT NULL,
  `sync_mode` enum('pull','push_webhook') NOT NULL DEFAULT 'push_webhook',
  `sync_config` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT 'sheet_id+range, pipeline/stage mapping, etc.' CHECK (json_valid(`sync_config`)),
  `webhook_token` varchar(64) DEFAULT NULL COMMENT 'token para validar webhooks entrantes (push)',
  `status` enum('active','disabled','error') NOT NULL DEFAULT 'active',
  `last_sync_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `fk_crm_lead_sources_tenant` (`tenant_id`),
  KEY `fk_crm_lead_sources_integration` (`tenant_integration_id`),
  CONSTRAINT `fk_crm_lead_sources_integration` FOREIGN KEY (`tenant_integration_id`) REFERENCES `tenant_integrations` (`id`),
  CONSTRAINT `fk_crm_lead_sources_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `crm_lead_sources`
--

LOCK TABLES `crm_lead_sources` WRITE;
/*!40000 ALTER TABLE `crm_lead_sources` DISABLE KEYS */;
/*!40000 ALTER TABLE `crm_lead_sources` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `crm_leads`
--

DROP TABLE IF EXISTS `crm_leads`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `crm_leads` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `contact_id` int(11) NOT NULL,
  `source_id` int(11) DEFAULT NULL,
  `campaign_id` int(11) DEFAULT NULL COMMENT 'voice_campaigns.id si el lead esta asignado a una campana de voz',
  `external_id` varchar(150) DEFAULT NULL COMMENT 'id del lead en el sistema de origen',
  `status` enum('new','ready_to_call','calling','contacted','no_answer','converted','opted_out','discarded') NOT NULL DEFAULT 'new',
  `stage` varchar(60) DEFAULT NULL COMMENT 'etapa de pipeline (libre por tenant)',
  `score` int(11) DEFAULT NULL,
  `notes` text DEFAULT NULL,
  `last_contact_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `fk_crm_leads_contact` (`contact_id`),
  KEY `fk_crm_leads_campaign` (`campaign_id`),
  KEY `idx_crm_leads_tenant_status` (`tenant_id`,`status`),
  KEY `idx_crm_leads_source_external` (`source_id`,`external_id`),
  CONSTRAINT `fk_crm_leads_campaign` FOREIGN KEY (`campaign_id`) REFERENCES `voice_campaigns` (`id`),
  CONSTRAINT `fk_crm_leads_contact` FOREIGN KEY (`contact_id`) REFERENCES `contacts` (`id`),
  CONSTRAINT `fk_crm_leads_source` FOREIGN KEY (`source_id`) REFERENCES `crm_lead_sources` (`id`),
  CONSTRAINT `fk_crm_leads_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `crm_leads`
--

LOCK TABLES `crm_leads` WRITE;
/*!40000 ALTER TABLE `crm_leads` DISABLE KEYS */;
/*!40000 ALTER TABLE `crm_leads` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `domain_events`
--

DROP TABLE IF EXISTS `domain_events`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `domain_events` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `module_key` varchar(40) NOT NULL COMMENT 'core, voice_ai, crm, ...',
  `event_type` varchar(80) NOT NULL COMMENT 'ej. contact.created, voice.call.completed',
  `aggregate_type` varchar(40) DEFAULT NULL,
  `aggregate_id` bigint(20) DEFAULT NULL,
  `payload` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`payload`)),
  `created_at` datetime(3) NOT NULL DEFAULT current_timestamp(3),
  `processed_at` datetime(3) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_domain_events_pending` (`processed_at`,`id`),
  KEY `idx_domain_events_tenant_type` (`tenant_id`,`event_type`),
  CONSTRAINT `fk_domain_events_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=4 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `domain_events`
--

LOCK TABLES `domain_events` WRITE;
/*!40000 ALTER TABLE `domain_events` DISABLE KEYS */;
INSERT INTO `domain_events` VALUES
(1,1,'voice_ai','voice.agent.created','agent',1,'{\"agent_id\": 1, \"name\": \"Call Center Mexico\", \"country\": \"MX\", \"language\": \"es\", \"voice_provider\": \"elevenlabs\", \"status\": \"draft\", \"has_system_prompt\": true, \"has_first_message\": true, \"external_agent_linked\": false}','2026-09-03 09:37:32.887',NULL),
(2,1,'voice_ai','voice.agent.updated','agent',1,'{\"agent_id\": 1, \"changed_fields\": [\"country\", \"description\", \"external_agent_id\", \"first_message\", \"language\", \"name\", \"status\", \"system_prompt\", \"voice_id\", \"voice_provider\"], \"prompt_changed\": true, \"first_message_changed\": true, \"external_agent_changed\": true, \"name\": \"Call Center Mexico\", \"country\": \"MX\", \"language\": \"es\", \"voice_provider\": \"elevenlabs\", \"status\": \"draft\"}','2026-09-03 09:37:59.304',NULL),
(3,1,'voice_ai','voice.agent.updated','agent',1,'{\"agent_id\": 1, \"changed_fields\": [\"country\", \"description\", \"external_agent_id\", \"first_message\", \"language\", \"name\", \"status\", \"system_prompt\", \"voice_id\", \"voice_provider\"], \"prompt_changed\": true, \"first_message_changed\": true, \"external_agent_changed\": true, \"name\": \"Call Center Mexico\", \"country\": \"MX\", \"language\": \"es\", \"voice_provider\": \"elevenlabs\", \"status\": \"draft\"}','2026-09-03 13:01:38.098',NULL);
/*!40000 ALTER TABLE `domain_events` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `integration_providers`
--

DROP TABLE IF EXISTS `integration_providers`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `integration_providers` (
  `provider_key` varchar(40) NOT NULL,
  `name` varchar(120) NOT NULL,
  `category` enum('crm','messaging','email','calendar','payments','telephony','voice','custom') NOT NULL,
  `auth_type` enum('oauth2','api_key','webhook_token','none') NOT NULL DEFAULT 'api_key',
  `capabilities` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT 'ej. ["contacts.pull","contacts.push","messages.send"]' CHECK (json_valid(`capabilities`)),
  `status` enum('available','beta','planned','deprecated') NOT NULL DEFAULT 'planned',
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`provider_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `integration_providers`
--

LOCK TABLES `integration_providers` WRITE;
/*!40000 ALTER TABLE `integration_providers` DISABLE KEYS */;
INSERT INTO `integration_providers` VALUES
('custom_api','API personalizada','custom','webhook_token','[\"contacts.push\",\"events.push\"]','available','2026-09-02 12:41:30'),
('elevenlabs','ElevenLabs','voice','api_key','[\"voice.tts\",\"voice.agent\"]','available','2026-09-02 12:41:30'),
('gmail','Gmail','email','oauth2','[\"email.send\",\"email.read\"]','planned','2026-09-02 12:41:30'),
('gohighlevel','GoHighLevel','crm','api_key','[\"contacts.pull\",\"contacts.push\"]','planned','2026-09-02 12:41:30'),
('google_calendar','Google Calendar','calendar','oauth2','[\"calendar.read\",\"calendar.write\"]','planned','2026-09-02 12:41:30'),
('google_sheets','Google Sheets','crm','oauth2','[\"contacts.pull\"]','available','2026-09-02 12:41:30'),
('hubspot','HubSpot','crm','oauth2','[\"contacts.pull\",\"contacts.push\",\"deals.update\"]','planned','2026-09-02 12:41:30'),
('meta_whatsapp','Meta WhatsApp Cloud API','messaging','api_key','[\"messages.send\",\"messages.receive\",\"templates.send\"]','planned','2026-09-02 12:41:30'),
('outlook','Microsoft Outlook','email','oauth2','[\"email.send\",\"email.read\"]','planned','2026-09-02 12:41:30'),
('outlook_calendar','Outlook Calendar','calendar','oauth2','[\"calendar.read\",\"calendar.write\"]','planned','2026-09-02 12:41:30'),
('pipedrive','Pipedrive','crm','api_key','[\"contacts.pull\",\"contacts.push\",\"deals.update\"]','planned','2026-09-02 12:41:30'),
('salesforce','Salesforce','crm','oauth2','[\"contacts.pull\",\"contacts.push\"]','planned','2026-09-02 12:41:30'),
('sip_trunk','Proveedor SIP (trunk)','telephony','none','[\"calls.outbound\"]','available','2026-09-02 12:41:30'),
('telegram','Telegram Bot','messaging','api_key','[\"messages.send\",\"messages.receive\"]','available','2026-09-02 12:41:30'),
('zoho','Zoho CRM','crm','oauth2','[\"contacts.pull\",\"contacts.push\"]','planned','2026-09-02 12:41:30');
/*!40000 ALTER TABLE `integration_providers` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `plan_entitlements`
--

DROP TABLE IF EXISTS `plan_entitlements`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `plan_entitlements` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `plan_id` int(11) NOT NULL,
  `service_key` varchar(40) NOT NULL,
  `included_quantity` decimal(14,4) NOT NULL DEFAULT 0.0000,
  `overage_price_usd` decimal(10,6) NOT NULL DEFAULT 0.000000 COMMENT 'precio por unidad excedente',
  `hard_limit` tinyint(1) NOT NULL DEFAULT 0 COMMENT '1 = bloquear al agotar en vez de cobrar excedente',
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_plan_entitlements` (`plan_id`,`service_key`),
  KEY `fk_plan_entitlements_service` (`service_key`),
  CONSTRAINT `fk_plan_entitlements_plan` FOREIGN KEY (`plan_id`) REFERENCES `plans` (`id`),
  CONSTRAINT `fk_plan_entitlements_service` FOREIGN KEY (`service_key`) REFERENCES `billable_services` (`service_key`)
) ENGINE=InnoDB AUTO_INCREMENT=16 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `plan_entitlements`
--

LOCK TABLES `plan_entitlements` WRITE;
/*!40000 ALTER TABLE `plan_entitlements` DISABLE KEYS */;
INSERT INTO `plan_entitlements` VALUES
(1,1,'ai_tokens_in',999999999.0000,0.000000,0),
(2,1,'ai_tokens_out',999999999.0000,0.000000,0),
(3,1,'automation_runs',999999999.0000,0.000000,0),
(4,1,'conversations',999999999.0000,0.000000,0),
(5,1,'email_sends',999999999.0000,0.000000,0),
(6,1,'seats',999999999.0000,0.000000,0),
(7,1,'voice_minutes',999999999.0000,0.000000,0),
(8,1,'wa_conversations',999999999.0000,0.000000,0),
(9,1,'wa_messages',999999999.0000,0.000000,0);
/*!40000 ALTER TABLE `plan_entitlements` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `plans`
--

DROP TABLE IF EXISTS `plans`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `plans` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `slug` varchar(64) NOT NULL,
  `name` varchar(160) NOT NULL,
  `price_usd` decimal(10,2) NOT NULL DEFAULT 0.00,
  `billing_period` enum('monthly','yearly') NOT NULL DEFAULT 'monthly',
  `status` enum('active','deprecated') NOT NULL DEFAULT 'active',
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `slug` (`slug`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `plans`
--

LOCK TABLES `plans` WRITE;
/*!40000 ALTER TABLE `plans` DISABLE KEYS */;
INSERT INTO `plans` VALUES
(1,'internal-unlimited','Internal Unlimited (validacion)',0.00,'monthly','active','2026-09-02 12:41:30');
/*!40000 ALTER TABLE `plans` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `schema_migrations`
--

DROP TABLE IF EXISTS `schema_migrations`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `schema_migrations` (
  `version` varchar(80) NOT NULL,
  `applied_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`version`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `schema_migrations`
--

LOCK TABLES `schema_migrations` WRITE;
/*!40000 ALTER TABLE `schema_migrations` DISABLE KEYS */;
INSERT INTO `schema_migrations` VALUES
('001_core_tenants_users','2026-09-02 12:41:30'),
('002_core_contacts_events','2026-09-02 12:41:30'),
('003_core_billing','2026-09-02 12:41:30'),
('004_core_integrations_agents','2026-09-02 12:41:30'),
('005_module_voice_ai','2026-09-02 12:41:30'),
('006_module_crm','2026-09-02 12:41:30'),
('007_seed_catalogs','2026-09-02 12:41:30'),
('008_voice_campaigns_extend','2026-09-02 13:52:00'),
('009_agents_extend','2026-09-03 06:10:04'),
('010_agents_country_first_message','2026-09-03 08:44:44'),
('011_agents_system_prompt_mediumtext','2026-09-03 09:23:50'),
('012_ai_provider_accounts','2026-09-03 14:03:41'),
('013_agents_provider_links','2026-09-03 14:03:41'),
('014_voice_catalog','2026-09-03 14:03:41'),
('015_tenant_wallets_ledger','2026-09-03 14:03:41'),
('016_seed_provider_accounts','2026-09-03 14:03:41'),
('017_disable_unconfigured_managed_provider','2026-09-04 06:18:53');
/*!40000 ALTER TABLE `schema_migrations` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `subscriptions`
--

DROP TABLE IF EXISTS `subscriptions`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `subscriptions` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `plan_id` int(11) NOT NULL,
  `status` enum('active','past_due','canceled','trialing') NOT NULL DEFAULT 'active',
  `starts_at` date DEFAULT NULL,
  `renews_at` date DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `fk_subscriptions_plan` (`plan_id`),
  KEY `idx_subscriptions_tenant` (`tenant_id`),
  CONSTRAINT `fk_subscriptions_plan` FOREIGN KEY (`plan_id`) REFERENCES `plans` (`id`),
  CONSTRAINT `fk_subscriptions_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `subscriptions`
--

LOCK TABLES `subscriptions` WRITE;
/*!40000 ALTER TABLE `subscriptions` DISABLE KEYS */;
INSERT INTO `subscriptions` VALUES
(1,1,1,'active',NULL,NULL,'2026-09-02 12:51:18');
/*!40000 ALTER TABLE `subscriptions` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `tenant_integrations`
--

DROP TABLE IF EXISTS `tenant_integrations`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `tenant_integrations` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `provider_key` varchar(40) NOT NULL,
  `label` varchar(100) DEFAULT NULL,
  `config_ref` varchar(160) NOT NULL COMMENT 'nombre del secreto (env/vault). NUNCA el valor',
  `settings` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT 'configuracion NO sensible (ids de cuenta, pipeline, etc.)' CHECK (json_valid(`settings`)),
  `status` enum('active','disabled','error') NOT NULL DEFAULT 'active',
  `last_sync_at` datetime DEFAULT NULL,
  `last_error` text DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `fk_tenant_integrations_provider` (`provider_key`),
  KEY `idx_tenant_integrations_tenant` (`tenant_id`,`provider_key`),
  CONSTRAINT `fk_tenant_integrations_provider` FOREIGN KEY (`provider_key`) REFERENCES `integration_providers` (`provider_key`),
  CONSTRAINT `fk_tenant_integrations_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `tenant_integrations`
--

LOCK TABLES `tenant_integrations` WRITE;
/*!40000 ALTER TABLE `tenant_integrations` DISABLE KEYS */;
/*!40000 ALTER TABLE `tenant_integrations` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `tenant_modules`
--

DROP TABLE IF EXISTS `tenant_modules`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `tenant_modules` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `module_key` varchar(40) NOT NULL,
  `status` enum('enabled','disabled') NOT NULL DEFAULT 'enabled',
  `config` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`config`)),
  `enabled_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_tenant_modules` (`tenant_id`,`module_key`),
  CONSTRAINT `fk_tenant_modules_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=3 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `tenant_modules`
--

LOCK TABLES `tenant_modules` WRITE;
/*!40000 ALTER TABLE `tenant_modules` DISABLE KEYS */;
INSERT INTO `tenant_modules` VALUES
(1,1,'voice_ai','enabled',NULL,'2026-09-02 12:51:18'),
(2,1,'crm','enabled',NULL,'2026-09-02 12:51:18');
/*!40000 ALTER TABLE `tenant_modules` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `tenant_wallets`
--

DROP TABLE IF EXISTS `tenant_wallets`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `tenant_wallets` (
  `tenant_id` int(11) NOT NULL,
  `currency` varchar(3) NOT NULL DEFAULT 'USD',
  `balance` decimal(14,4) NOT NULL DEFAULT 0.0000,
  `reserved_balance` decimal(14,4) NOT NULL DEFAULT 0.0000,
  `status` enum('active','suspended') NOT NULL DEFAULT 'active',
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`tenant_id`),
  CONSTRAINT `fk_tenant_wallets_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`),
  CONSTRAINT `chk_tenant_wallets_non_negative` CHECK (`balance` >= 0 and `reserved_balance` >= 0)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `tenant_wallets`
--

LOCK TABLES `tenant_wallets` WRITE;
/*!40000 ALTER TABLE `tenant_wallets` DISABLE KEYS */;
/*!40000 ALTER TABLE `tenant_wallets` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `tenants`
--

DROP TABLE IF EXISTS `tenants`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `tenants` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `slug` varchar(64) NOT NULL,
  `name` varchar(160) NOT NULL,
  `status` enum('active','suspended','trial') NOT NULL DEFAULT 'active',
  `plan_id` int(11) DEFAULT NULL,
  `billing_start_date` date DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `slug` (`slug`),
  KEY `fk_tenants_plan` (`plan_id`),
  CONSTRAINT `fk_tenants_plan` FOREIGN KEY (`plan_id`) REFERENCES `plans` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `tenants`
--

LOCK TABLES `tenants` WRITE;
/*!40000 ALTER TABLE `tenants` DISABLE KEYS */;
INSERT INTO `tenants` VALUES
(1,'tenant-inicial','Tenant inicial de validacion','active',1,NULL,'2026-09-02 12:51:18','2026-09-02 12:51:18');
/*!40000 ALTER TABLE `tenants` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `usage_events`
--

DROP TABLE IF EXISTS `usage_events`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `usage_events` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `service_key` varchar(40) NOT NULL,
  `quantity` decimal(14,4) NOT NULL,
  `cost_usd` decimal(12,6) NOT NULL DEFAULT 0.000000 COMMENT 'lo que pagamos al proveedor',
  `price_usd` decimal(12,6) NOT NULL DEFAULT 0.000000 COMMENT 'lo que cobramos al tenant',
  `reference_id` varchar(150) NOT NULL COMMENT 'idempotencia: cdr uniqueid, message id, run id...',
  `occurred_at` datetime NOT NULL,
  `metadata` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`metadata`)),
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_usage_events_ref` (`tenant_id`,`service_key`,`reference_id`),
  KEY `fk_usage_events_service` (`service_key`),
  KEY `idx_usage_events_tenant_period` (`tenant_id`,`service_key`,`occurred_at`),
  CONSTRAINT `fk_usage_events_service` FOREIGN KEY (`service_key`) REFERENCES `billable_services` (`service_key`),
  CONSTRAINT `fk_usage_events_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `usage_events`
--

LOCK TABLES `usage_events` WRITE;
/*!40000 ALTER TABLE `usage_events` DISABLE KEYS */;
/*!40000 ALTER TABLE `usage_events` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `users`
--

DROP TABLE IF EXISTS `users`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `username` varchar(64) NOT NULL,
  `email` varchar(160) DEFAULT NULL,
  `password_hash` varchar(255) NOT NULL,
  `role` enum('superadmin','owner','admin','operator','support','viewer') NOT NULL DEFAULT 'viewer',
  `status` enum('active','disabled') NOT NULL DEFAULT 'active',
  `last_login_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_users_tenant_username` (`tenant_id`,`username`),
  CONSTRAINT `fk_users_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `users`
--

LOCK TABLES `users` WRITE;
/*!40000 ALTER TABLE `users` DISABLE KEYS */;
INSERT INTO `users` VALUES
(1,1,'owner',NULL,'scrypt:32768:8:1$HYWAqOy5cfcZJjUW$2e1fc463a7f10df44d95e4b2310ba88d7f11b728be1cbae0de15bf8b95a47a8a9f18b318de41c9825e9ca36597cd499f3f5dea377ac6b4ada09751f6724adcfe','owner','active','2026-09-03 06:53:34','2026-09-02 12:51:18','2026-09-03 06:53:34');
/*!40000 ALTER TABLE `users` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `voice_calls`
--

DROP TABLE IF EXISTS `voice_calls`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `voice_calls` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `campaign_id` int(11) DEFAULT NULL,
  `contact_id` int(11) DEFAULT NULL,
  `agent_id` int(11) DEFAULT NULL,
  `direction` enum('outbound','inbound') NOT NULL DEFAULT 'outbound',
  `cdr_uniqueid` varchar(150) DEFAULT NULL COMMENT 'referencia a asterisk.cdr.uniqueid. NUNCA FK real: cdr es de Asterisk',
  `phone_e164` varchar(20) DEFAULT NULL,
  `started_at` datetime DEFAULT NULL,
  `duration_seconds` int(11) DEFAULT NULL,
  `billsec` int(11) DEFAULT NULL COMMENT 'segundos facturables (igual que cdr.billsec)',
  `status` varchar(30) DEFAULT NULL COMMENT 'ANSWERED, NO ANSWER, BUSY, FAILED...',
  `outcome` varchar(40) DEFAULT NULL COMMENT 'resultado de negocio: interested, callback, not_interested...',
  `recording_reference` varchar(255) DEFAULT NULL,
  `transcript_reference` varchar(255) DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `fk_voice_calls_campaign` (`campaign_id`),
  KEY `fk_voice_calls_contact` (`contact_id`),
  KEY `fk_voice_calls_agent` (`agent_id`),
  KEY `idx_voice_calls_cdr` (`cdr_uniqueid`),
  KEY `idx_voice_calls_tenant_started` (`tenant_id`,`started_at`),
  CONSTRAINT `fk_voice_calls_agent` FOREIGN KEY (`agent_id`) REFERENCES `agents` (`id`),
  CONSTRAINT `fk_voice_calls_campaign` FOREIGN KEY (`campaign_id`) REFERENCES `voice_campaigns` (`id`),
  CONSTRAINT `fk_voice_calls_contact` FOREIGN KEY (`contact_id`) REFERENCES `contacts` (`id`),
  CONSTRAINT `fk_voice_calls_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `voice_calls`
--

LOCK TABLES `voice_calls` WRITE;
/*!40000 ALTER TABLE `voice_calls` DISABLE KEYS */;
/*!40000 ALTER TABLE `voice_calls` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `voice_campaigns`
--

DROP TABLE IF EXISTS `voice_campaigns`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `voice_campaigns` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `agent_id` int(11) NOT NULL,
  `name` varchar(120) NOT NULL,
  `description` text DEFAULT NULL,
  `country` varchar(2) NOT NULL COMMENT 'ISO-3166-1 alpha-2, ej. MX, IN, AE',
  `timezone` varchar(50) NOT NULL,
  `schedule_start` time DEFAULT NULL,
  `schedule_end` time DEFAULT NULL,
  `schedule_days` varchar(20) DEFAULT NULL COMMENT 'ej. mon-fri',
  `daily_limit` int(11) DEFAULT NULL,
  `status` enum('draft','active','paused','archived','finished') NOT NULL DEFAULT 'draft',
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `fk_voice_campaigns_agent` (`agent_id`),
  KEY `idx_voice_campaigns_tenant_status` (`tenant_id`,`status`),
  CONSTRAINT `fk_voice_campaigns_agent` FOREIGN KEY (`agent_id`) REFERENCES `agents` (`id`),
  CONSTRAINT `fk_voice_campaigns_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `voice_campaigns`
--

LOCK TABLES `voice_campaigns` WRITE;
/*!40000 ALTER TABLE `voice_campaigns` DISABLE KEYS */;
/*!40000 ALTER TABLE `voice_campaigns` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `voice_catalog`
--

DROP TABLE IF EXISTS `voice_catalog`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `voice_catalog` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `platform_voice_id` varchar(40) NOT NULL COMMENT 'ID publico, propio de la plataforma — lo unico que ve el cliente',
  `provider_key` varchar(40) NOT NULL COMMENT 'INTERNO — nunca se expone al tenant',
  `provider_account_id` int(11) DEFAULT NULL COMMENT 'INTERNO — NULL = entrada de catalogo compartida entre cuentas managed',
  `provider_voice_id` varchar(160) NOT NULL COMMENT 'INTERNO — id real de la voz en el proveedor, nunca se expone al tenant',
  `name` varchar(120) NOT NULL,
  `language` varchar(10) NOT NULL,
  `locale` varchar(10) DEFAULT NULL,
  `accent` varchar(60) DEFAULT NULL,
  `gender` varchar(20) DEFAULT NULL,
  `age` varchar(20) DEFAULT NULL,
  `category` varchar(60) DEFAULT NULL,
  `use_case` varchar(120) DEFAULT NULL,
  `description` text DEFAULT NULL,
  `preview_url` varchar(500) DEFAULT NULL,
  `available` tinyint(1) NOT NULL DEFAULT 1,
  `provider_cost_multiplier` decimal(6,3) NOT NULL DEFAULT 1.000 COMMENT 'INTERNO — nunca se expone al tenant',
  `customer_price_multiplier` decimal(6,3) NOT NULL DEFAULT 1.000 COMMENT 'multiplicador que SI puede reflejarse en el precio mostrado al tenant',
  `pricing_tier` enum('standard','premium','custom') NOT NULL DEFAULT 'standard',
  `disable_at` datetime DEFAULT NULL COMMENT 'si el proveedor avisa notice period de baja de la voz',
  `metadata` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`metadata`)),
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_voice_catalog_platform_voice_id` (`platform_voice_id`),
  KEY `fk_voice_catalog_provider_account` (`provider_account_id`),
  KEY `idx_voice_catalog_language` (`language`,`available`),
  KEY `idx_voice_catalog_provider` (`provider_key`,`provider_voice_id`),
  CONSTRAINT `fk_voice_catalog_provider` FOREIGN KEY (`provider_key`) REFERENCES `integration_providers` (`provider_key`),
  CONSTRAINT `fk_voice_catalog_provider_account` FOREIGN KEY (`provider_account_id`) REFERENCES `ai_provider_accounts` (`id`),
  CONSTRAINT `CONSTRAINT_1` CHECK (json_valid(`metadata`))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `voice_catalog`
--

LOCK TABLES `voice_catalog` WRITE;
/*!40000 ALTER TABLE `voice_catalog` DISABLE KEYS */;
/*!40000 ALTER TABLE `voice_catalog` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `wallet_ledger`
--

DROP TABLE IF EXISTS `wallet_ledger`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `wallet_ledger` (
  `id` bigint(20) NOT NULL AUTO_INCREMENT,
  `tenant_id` int(11) NOT NULL,
  `entry_type` enum('credit','debit','reserve','release','settlement','refund','adjustment') NOT NULL,
  `amount` decimal(14,4) NOT NULL,
  `currency` varchar(3) NOT NULL DEFAULT 'USD',
  `reference_type` varchar(40) DEFAULT NULL COMMENT 'ej. usage_event, manual_topup, ...',
  `reference_id` varchar(150) DEFAULT NULL,
  `idempotency_key` varchar(150) NOT NULL,
  `metadata` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL CHECK (json_valid(`metadata`)),
  `created_at` datetime(3) NOT NULL DEFAULT current_timestamp(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_wallet_ledger_idempotency` (`tenant_id`,`idempotency_key`),
  KEY `idx_wallet_ledger_tenant_created` (`tenant_id`,`created_at`),
  KEY `idx_wallet_ledger_reference` (`reference_type`,`reference_id`),
  CONSTRAINT `fk_wallet_ledger_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`),
  CONSTRAINT `CONSTRAINT_1` CHECK (json_valid(`metadata`))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `wallet_ledger`
--

LOCK TABLES `wallet_ledger` WRITE;
/*!40000 ALTER TABLE `wallet_ledger` DISABLE KEYS */;
/*!40000 ALTER TABLE `wallet_ledger` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-09-04  7:51:36
