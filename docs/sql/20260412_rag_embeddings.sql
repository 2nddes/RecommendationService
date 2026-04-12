-- RAG cold storage + async embedding queue
-- Apply manually on MySQL 8.x

CREATE TABLE IF NOT EXISTS `movie_embeddings` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `movie_id` BIGINT UNSIGNED NOT NULL COMMENT '关联的主业务电影ID',
  `chunk_text` TEXT NOT NULL COMMENT '用于生成向量的原始完整文本',
  `embedding_vector` BLOB NOT NULL COMMENT '向量数据（二进制格式序列化存储）',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_movie_id` (`movie_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='电影向量库冷存储表';

CREATE TABLE IF NOT EXISTS `rag_embedding_job` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '任务ID',
  `movie_id` BIGINT UNSIGNED NOT NULL COMMENT '待生成向量的电影ID',
  `status` ENUM('pending','processing','completed','failed') NOT NULL DEFAULT 'pending' COMMENT '任务状态',
  `retry_count` INT UNSIGNED NOT NULL DEFAULT 0 COMMENT '已重试次数',
  `error` VARCHAR(1000) NULL DEFAULT NULL COMMENT '最后一次错误信息',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_status_created` (`status`, `created_at`),
  KEY `idx_movie_id` (`movie_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='RAG embedding 异步任务队列';
