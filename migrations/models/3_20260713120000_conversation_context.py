from tortoise import BaseDBAsyncClient


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `chat_sessions`
            ADD COLUMN `summary` LONGTEXT NULL AFTER `title`,
            ADD COLUMN `summarized_turn_count` INT NOT NULL DEFAULT 0 AFTER `summary`,
            ADD COLUMN `summarized_through_turn_id` INT NULL AFTER `summarized_turn_count`,
            ADD COLUMN `summary_updated_at` DATETIME(6) NULL AFTER `summarized_through_turn_id`,
            ADD KEY `idx_chat_sessions_updated_at` (`updated_at`);

        ALTER TABLE `agent_tasks`
            ADD KEY `idx_agent_tasks_session_status_created` (`session_id`, `status`, `created_at`);
    """


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE `agent_tasks`
            DROP INDEX `idx_agent_tasks_session_status_created`;

        ALTER TABLE `chat_sessions`
            DROP INDEX `idx_chat_sessions_updated_at`,
            DROP COLUMN `summary_updated_at`,
            DROP COLUMN `summarized_through_turn_id`,
            DROP COLUMN `summarized_turn_count`,
            DROP COLUMN `summary`;
    """
