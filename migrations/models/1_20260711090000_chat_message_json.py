from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `chat_messages`
            ADD COLUMN `task_id` VARCHAR(64) NULL AFTER `session_id`,
            ADD COLUMN `message_json` JSON NULL AFTER `task_id`;

        UPDATE `chat_messages`
        SET
            `task_id` = COALESCE(
                NULLIF(JSON_UNQUOTE(JSON_EXTRACT(`metadata_json`, '$.context.task_id')), 'null'),
                NULLIF(JSON_UNQUOTE(JSON_EXTRACT(`metadata_json`, '$.task_id')), 'null')
            ),
            `message_json` = JSON_OBJECT(
                'role', `role`,
                'content', `content`
            );

        UPDATE `chat_messages` AS `message`
        SET `message`.`task_id` = (
            SELECT `task`.`task_id`
            FROM `agent_tasks` AS `task`
            WHERE `task`.`session_id` = `message`.`session_id`
              AND `task`.`created_at` <= `message`.`created_at`
            ORDER BY `task`.`created_at` DESC
            LIMIT 1
        )
        WHERE `message`.`task_id` IS NULL;

        UPDATE `agent_tasks` AS `task`
        SET `task`.`metadata_json` = (
            SELECT `message`.`metadata_json`
            FROM `chat_messages` AS `message`
            WHERE `message`.`task_id` = `task`.`task_id`
              AND JSON_UNQUOTE(
                    JSON_EXTRACT(`message`.`message_json`, '$.role')
                  ) = 'assistant'
              AND JSON_LENGTH(`message`.`metadata_json`) > 0
            ORDER BY `message`.`created_at` DESC
            LIMIT 1
        )
        WHERE JSON_LENGTH(`task`.`metadata_json`) = 0
          AND EXISTS (
              SELECT 1
              FROM `chat_messages` AS `message`
              WHERE `message`.`task_id` = `task`.`task_id`
                AND JSON_UNQUOTE(
                      JSON_EXTRACT(`message`.`message_json`, '$.role')
                    ) = 'assistant'
                AND JSON_LENGTH(`message`.`metadata_json`) > 0
          );

        ALTER TABLE `chat_messages`
            MODIFY COLUMN `message_json` JSON NOT NULL,
            DROP COLUMN `role`,
            DROP COLUMN `content`,
            DROP COLUMN `metadata_json`,
            ADD KEY `idx_chat_messages_task_id` (`task_id`);
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `chat_messages`
            ADD COLUMN `role` VARCHAR(20) NULL AFTER `session_id`,
            ADD COLUMN `content` LONGTEXT NULL AFTER `role`,
            ADD COLUMN `metadata_json` JSON NULL AFTER `message_type`;

        UPDATE `chat_messages` AS `message`
        LEFT JOIN `agent_tasks` AS `task`
            ON `task`.`task_id` = `message`.`task_id`
        SET
            `message`.`role` = JSON_UNQUOTE(
                JSON_EXTRACT(`message`.`message_json`, '$.role')
            ),
            `message`.`content` = JSON_UNQUOTE(
                JSON_EXTRACT(`message`.`message_json`, '$.content')
            ),
            `message`.`metadata_json` = COALESCE(
                `task`.`metadata_json`,
                JSON_OBJECT()
            );

        ALTER TABLE `chat_messages`
            MODIFY COLUMN `role` VARCHAR(20) NOT NULL,
            MODIFY COLUMN `content` LONGTEXT NOT NULL,
            MODIFY COLUMN `metadata_json` JSON NOT NULL,
            DROP INDEX `idx_chat_messages_task_id`,
            DROP COLUMN `task_id`,
            DROP COLUMN `message_json`;
    """
