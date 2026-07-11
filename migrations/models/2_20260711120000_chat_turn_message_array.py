from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        SET SESSION group_concat_max_len = 16777216;

        ALTER TABLE `chat_messages`
            ADD COLUMN `updated_at` DATETIME(6) NULL AFTER `created_at`;

        UPDATE `chat_messages`
        SET `task_id` = CONCAT('legacy-', `id`)
        WHERE `task_id` IS NULL OR `task_id` = '';

        UPDATE `chat_messages` AS `retained`
        JOIN (
            SELECT
                `task_id`,
                MIN(`id`) AS `retained_id`,
                CAST(
                    CONCAT(
                        '[',
                        GROUP_CONCAT(
                            CAST(`message_json` AS CHAR)
                            ORDER BY `created_at`, `id`
                            SEPARATOR ','
                        ),
                        ']'
                    ) AS JSON
                ) AS `messages`,
                MAX(`created_at`) AS `updated_at`
            FROM `chat_messages`
            GROUP BY `task_id`
        ) AS `grouped`
            ON `grouped`.`retained_id` = `retained`.`id`
        SET
            `retained`.`message_json` = `grouped`.`messages`,
            `retained`.`updated_at` = `grouped`.`updated_at`;

        DELETE `duplicate`
        FROM `chat_messages` AS `duplicate`
        JOIN `chat_messages` AS `retained`
          ON `retained`.`task_id` = `duplicate`.`task_id`
         AND `retained`.`id` < `duplicate`.`id`;

        UPDATE `chat_messages`
        SET `updated_at` = COALESCE(`updated_at`, `created_at`);

        ALTER TABLE `chat_messages`
            MODIFY COLUMN `task_id` VARCHAR(64) NOT NULL,
            MODIFY COLUMN `message_json` JSON NOT NULL,
            MODIFY COLUMN `updated_at` DATETIME(6) NOT NULL,
            DROP COLUMN `message_type`,
            DROP INDEX `idx_chat_messages_task_id`,
            ADD UNIQUE KEY `uid_chat_messages_task_id` (`task_id`);
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE `chat_messages_expanded` (
            `id` INT NOT NULL PRIMARY KEY AUTO_INCREMENT,
            `session_id` VARCHAR(64) NOT NULL,
            `task_id` VARCHAR(64) NULL,
            `message_json` JSON NOT NULL,
            `message_type` VARCHAR(32) NOT NULL,
            `created_at` DATETIME(6) NOT NULL,
            KEY `idx_chat_messag_session_f7a29e` (`session_id`),
            KEY `idx_chat_messages_task_id` (`task_id`)
        ) CHARACTER SET utf8mb4;

        INSERT INTO `chat_messages_expanded` (
            `session_id`,
            `task_id`,
            `message_json`,
            `message_type`,
            `created_at`
        )
        SELECT
            `turn`.`session_id`,
            `turn`.`task_id`,
            `expanded`.`message_json`,
            'message',
            TIMESTAMPADD(
                MICROSECOND,
                `expanded`.`ordinality` - 1,
                `turn`.`created_at`
            )
        FROM `chat_messages` AS `turn`
        JOIN JSON_TABLE(
            `turn`.`message_json`,
            '$[*]' COLUMNS (
                `ordinality` FOR ORDINALITY,
                `message_json` JSON PATH '$'
            )
        ) AS `expanded`;

        DROP TABLE `chat_messages`;
        RENAME TABLE `chat_messages_expanded` TO `chat_messages`;
    """
