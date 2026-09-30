CREATE DATABASE IF NOT EXISTS asterisk;
USE asterisk;
CREATE TABLE IF NOT EXISTS `wf_call_followups` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `phone` varchar(20) NOT NULL,
  `lead_id` varchar(64) NOT NULL,
  `followup_id` varchar(64) NOT NULL,
  `provider` varchar(20) NOT NULL,
  `outcome` varchar(20) DEFAULT NULL,
  `call_status` varchar(20) DEFAULT NULL,
  `recording_synced` tinyint(4) DEFAULT 0,
  `created_at` timestamp NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  KEY `idx_phone` (`phone`),
  KEY `idx_created_at` (`created_at`),
  KEY `idx_recording_synced` (`recording_synced`)
) ENGINE=InnoDB AUTO_INCREMENT=3505 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
CREATE TABLE IF NOT EXISTS `stringee_calls` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `call_id` varchar(100) DEFAULT NULL,
  `conversation_id` varchar(255) DEFAULT NULL,
  `lead_id` varchar(64) DEFAULT NULL,
  `phone` varchar(32) DEFAULT NULL,
  `country` varchar(32) DEFAULT NULL,
  `answered` tinyint(1) DEFAULT 0,
  `duration_secs` int(11) DEFAULT 0,
  `start_time` bigint(20) DEFAULT NULL,
  `answer_time` bigint(20) DEFAULT NULL,
  `stop_time` bigint(20) DEFAULT NULL,
  `created_at` datetime DEFAULT current_timestamp(),
  `sheet_synced` tinyint(1) DEFAULT 0,
  `sheet_synced_recording` tinyint(1) DEFAULT 0,
  PRIMARY KEY (`id`),
  UNIQUE KEY `call_id` (`call_id`),
  KEY `idx_lead_id` (`lead_id`),
  KEY `idx_country` (`country`),
  KEY `idx_created_at` (`created_at`),
  KEY `idx_conversation_id` (`conversation_id`)
) ENGINE=InnoDB AUTO_INCREMENT=347010 DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
CREATE TABLE IF NOT EXISTS wf2_provider_config (provider varchar(32) PRIMARY KEY, enabled tinyint(1), updated_at datetime DEFAULT current_timestamp());
INSERT IGNORE INTO wf2_provider_config (provider, enabled) VALUES ('asterisk',1),('stringee',1);
