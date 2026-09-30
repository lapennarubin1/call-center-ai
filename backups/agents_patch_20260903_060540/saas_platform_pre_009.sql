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
  `type` enum('voice','chat','whatsapp','email','workflow') NOT NULL DEFAULT 'voice',
  `language` varchar(10) NOT NULL DEFAULT 'es',
  `model_provider` varchar(40) DEFAULT NULL COMMENT 'anthropic, openai, ...',
  `model_name` varchar(80) DEFAULT NULL,
  `voice_provider` varchar(40) DEFAULT NULL COMMENT 'elevenlabs, ... (solo type=voice)',
  `voice_id` varchar(100) DEFAULT NULL,
  `system_prompt` text DEFAULT NULL,
  `channel_config` longtext CHARACTER SET utf8mb4 COLLATE utf8mb4_bin DEFAULT NULL COMMENT 'config especifica del canal' CHECK (json_valid(`channel_config`)),
  `status` enum('active','disabled') NOT NULL DEFAULT 'active',
  `created_at` datetime NOT NULL DEFAULT current_timestamp(),
  `updated_at` datetime NOT NULL DEFAULT current_timestamp() ON UPDATE current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_agents_tenant_type` (`tenant_id`,`type`),
  CONSTRAINT `fk_agents_tenant` FOREIGN KEY (`tenant_id`) REFERENCES `tenants` (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `agents`
--

LOCK TABLES `agents` WRITE;
/*!40000 ALTER TABLE `agents` DISABLE KEYS */;
/*!40000 ALTER TABLE `agents` ENABLE KEYS */;
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
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `domain_events`
--

LOCK TABLES `domain_events` WRITE;
/*!40000 ALTER TABLE `domain_events` DISABLE KEYS */;
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
('008_voice_campaigns_extend','2026-09-02 13:52:00');
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
(1,1,'owner',NULL,'scrypt:32768:8:1$HYWAqOy5cfcZJjUW$2e1fc463a7f10df44d95e4b2310ba88d7f11b728be1cbae0de15bf8b95a47a8a9f18b318de41c9825e9ca36597cd499f3f5dea377ac6b4ada09751f6724adcfe','owner','active','2026-09-02 13:53:15','2026-09-02 12:51:18','2026-09-02 13:53:15');
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
-- Dumping routines for database 'saas_platform'
--
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2026-09-03  6:05:49
