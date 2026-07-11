from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS `chat_messages` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `session_id` VARCHAR(64) NOT NULL,
    `role` VARCHAR(20) NOT NULL,
    `content` LONGTEXT NOT NULL,
    `message_type` VARCHAR(32) NOT NULL,
    `metadata_json` JSON NOT NULL,
    `created_at` DATETIME(6) NOT NULL,
    KEY `idx_chat_messag_session_f7a29e` (`session_id`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `chat_sessions` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `session_id` VARCHAR(64) NOT NULL UNIQUE,
    `title` VARCHAR(255),
    `created_at` DATETIME(6) NOT NULL,
    `updated_at` DATETIME(6) NOT NULL,
    KEY `idx_chat_sessio_session_a5c17d` (`session_id`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `agent_tasks` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `task_id` VARCHAR(64) NOT NULL UNIQUE,
    `session_id` VARCHAR(64) NOT NULL,
    `task_type` VARCHAR(32) NOT NULL,
    `status` VARCHAR(32) NOT NULL,
    `instruction` LONGTEXT NOT NULL,
    `execution_mode` VARCHAR(16) NOT NULL,
    `template_name` VARCHAR(64),
    `input_file_id` VARCHAR(64),
    `output_file_id` VARCHAR(64),
    `result_summary` LONGTEXT,
    `error_message` LONGTEXT,
    `metadata_json` JSON NOT NULL,
    `created_at` DATETIME(6) NOT NULL,
    `updated_at` DATETIME(6) NOT NULL,
    KEY `idx_agent_tasks_task_id_daf934` (`task_id`),
    KEY `idx_agent_tasks_session_f1491b` (`session_id`),
    KEY `idx_agent_tasks_task_ty_f996b3` (`task_type`),
    KEY `idx_agent_tasks_status_48e0c2` (`status`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `stored_files` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `file_id` VARCHAR(64) NOT NULL UNIQUE,
    `original_name` VARCHAR(255) NOT NULL,
    `stored_path` LONGTEXT NOT NULL,
    `mime_type` VARCHAR(100) NOT NULL,
    `size_bytes` BIGINT NOT NULL,
    `file_role` VARCHAR(16) NOT NULL,
    `created_at` DATETIME(6) NOT NULL,
    KEY `idx_stored_file_file_id_0ba3ee` (`file_id`)
) CHARACTER SET utf8mb4;
CREATE TABLE IF NOT EXISTS `aerich` (
    `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
    `version` VARCHAR(255) NOT NULL,
    `app` VARCHAR(100) NOT NULL,
    `content` JSON NOT NULL
) CHARACTER SET utf8mb4;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        """
