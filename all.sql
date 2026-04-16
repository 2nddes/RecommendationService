/*
 Navicat Premium Dump SQL

 Source Server         : MovRecDB
 Source Server Type    : MySQL
 Source Server Version : 80407 (8.4.7)
 Source Host           : localhost:3306
 Source Schema         : movie_rec

 Target Server Type    : MySQL
 Target Server Version : 80407 (8.4.7)
 File Encoding         : 65001

 Date: 02/04/2026 10:45:26
*/

SET NAMES utf8mb4;
SET FOREIGN_KEY_CHECKS = 0;

-- ----------------------------
-- Table structure for comment_like
-- ----------------------------
DROP TABLE IF EXISTS `comment_like`;
CREATE TABLE `comment_like`  (
  `user_id` bigint NOT NULL COMMENT '点赞者',
  `comment_id` bigint NOT NULL COMMENT '被点赞评论',
  `create_at` datetime NULL DEFAULT NULL COMMENT '点赞时间',
  PRIMARY KEY (`user_id`, `comment_id`) USING BTREE,
  INDEX `comment_id`(`comment_id` ASC) USING BTREE,
  CONSTRAINT `comment_like_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE CASCADE,
  CONSTRAINT `comment_like_ibfk_2` FOREIGN KEY (`comment_id`) REFERENCES `movie_comment` (`comment_id`) ON DELETE CASCADE ON UPDATE CASCADE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for dict_language
-- ----------------------------
DROP TABLE IF EXISTS `dict_language`;
CREATE TABLE `dict_language`  (
  `lang_id` int NOT NULL AUTO_INCREMENT,
  `code` varchar(10) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL COMMENT 'ISO 639-1代码 (e.g., en, zh, ja)',
  `name_en` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '英文名',
  `name_cn` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '中文名',
  PRIMARY KEY (`lang_id`) USING BTREE,
  UNIQUE INDEX `uk_code`(`code` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 856 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '语言标准字典表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for dict_region
-- ----------------------------
DROP TABLE IF EXISTS `dict_region`;
CREATE TABLE `dict_region`  (
  `region_id` int NOT NULL AUTO_INCREMENT,
  `code` varchar(10) CHARACTER SET ascii COLLATE ascii_general_ci NOT NULL COMMENT 'ISO 3166-1代码 (e.g., US, CN, HK, GB)',
  `name_en` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '英文名',
  `name_cn` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '中文名',
  PRIMARY KEY (`region_id`) USING BTREE,
  UNIQUE INDEX `uk_code`(`code` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 512 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '国家地区标准字典表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for ops_task
-- ----------------------------
DROP TABLE IF EXISTS `ops_task`;
CREATE TABLE `ops_task`  (
  `id` bigint unsigned NOT NULL AUTO_INCREMENT COMMENT '任务主键ID',
  `task_ref_override` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '迁移旧任务时保留的稳定任务ID',
  `task_type` varchar(64) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '任务类型',
  `status` enum('pending','processing','completed','failed') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'pending' COMMENT '任务状态',
  `parent_task_id` bigint unsigned NULL DEFAULT NULL COMMENT '父任务主键ID',
  `retry_count` int unsigned NOT NULL DEFAULT 0 COMMENT '已重试次数',
  `error` varchar(1000) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '错误信息',
  `payload` json NULL COMMENT '任务输入与上下文(JSON)',
  `progress` json NULL COMMENT '任务进度(JSON)',
  `result` json NULL COMMENT '任务结果(JSON)',
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  `started_at` timestamp NULL DEFAULT NULL COMMENT '开始处理时间',
  `finished_at` timestamp NULL DEFAULT NULL COMMENT '完成时间',
  PRIMARY KEY (`id`) USING BTREE,
  UNIQUE INDEX `uk_task_ref_override`(`task_ref_override` ASC) USING BTREE,
  INDEX `idx_type_status_created`(`task_type` ASC, `status` ASC, `created_at` ASC) USING BTREE,
  INDEX `idx_parent_created`(`parent_task_id` ASC, `created_at` ASC) USING BTREE,
  INDEX `idx_status_created`(`status` ASC, `created_at` ASC) USING BTREE,
  CONSTRAINT `fk_ops_task_parent` FOREIGN KEY (`parent_task_id`) REFERENCES `ops_task` (`id`) ON DELETE SET NULL ON UPDATE RESTRICT
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '统一任务表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for movie
-- ----------------------------
DROP TABLE IF EXISTS `movie`;
CREATE TABLE `movie`  (
  `movie_id` bigint NOT NULL AUTO_INCREMENT COMMENT 'ID',
  `title` varchar(200) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '标题',
  `year` int NULL DEFAULT NULL COMMENT '年份',
  `release_date` date NULL DEFAULT NULL COMMENT '具体年月日',
  `duration_min` int NULL DEFAULT NULL COMMENT '时长(分钟)',
  `poster` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '海报URL',
  `summary` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT '简介',
  `ai_summary` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL COMMENT 'ai生成的简介',
  `rating_sum` bigint NOT NULL DEFAULT 0 COMMENT '评分总和',
  `rating_count` int NOT NULL DEFAULT 0 COMMENT '评分人数',
  `rating_1_count` int NOT NULL DEFAULT 0 COMMENT '评分1的人数',
  `rating_2_count` int NOT NULL DEFAULT 0,
  `rating_3_count` int NOT NULL DEFAULT 0,
  `rating_4_count` int NOT NULL DEFAULT 0,
  `rating_5_count` int NOT NULL DEFAULT 0,
  `rating_6_count` int NOT NULL DEFAULT 0,
  `rating_7_count` int NOT NULL DEFAULT 0,
  `rating_8_count` int NOT NULL DEFAULT 0,
  `rating_9_count` int NOT NULL DEFAULT 0,
  `rating_10_count` int NOT NULL DEFAULT 0,
  `status` enum('draft','published','offline') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT 'draft' COMMENT '状态',
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime NULL DEFAULT NULL,
  PRIMARY KEY (`movie_id`) USING BTREE,
  INDEX `idx_title`(`title` ASC) USING BTREE,
  INDEX `idx_year`(`year` ASC) USING BTREE,
  INDEX `idx_rating_avg`(`rating_sum` ASC) USING BTREE,
  INDEX `idx_status`(`status` ASC) USING BTREE,
  FULLTEXT INDEX `ft_title_summary`(`title`, `summary`)
) ENGINE = InnoDB AUTO_INCREMENT = 34782623 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '影视主表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for movie_comment
-- ----------------------------
DROP TABLE IF EXISTS `movie_comment`;
CREATE TABLE `movie_comment`  (
  `comment_id` bigint NOT NULL AUTO_INCREMENT COMMENT '全局唯一ID',
  `movie_id` bigint NOT NULL,
  `user_id` bigint NOT NULL,
  `content` text CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '评论内容',
  `root_id` bigint NULL DEFAULT NULL COMMENT '属于哪个根评论',
  `reply_to_user_id` bigint NULL DEFAULT NULL COMMENT '对哪个用户回复',
  `parent_id` bigint NULL DEFAULT NULL COMMENT '父评论ID(用于回复)',
  `like_count` int NULL DEFAULT 0 COMMENT '点赞数',
  `reply_count` int NULL DEFAULT 0 COMMENT '回复数',
  `is_top` tinyint(1) NULL DEFAULT 0 COMMENT '是否置顶',
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime NULL DEFAULT NULL,
  PRIMARY KEY (`comment_id`) USING BTREE,
  INDEX `idx_movie_id`(`movie_id` ASC) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE,
  INDEX `idx_parent_id`(`parent_id` ASC) USING BTREE,
  INDEX `idx_movie_created`(`movie_id` ASC, `created_at` ASC) USING BTREE,
  INDEX `idx_root_id`(`root_id` ASC) USING BTREE,
  INDEX `deleted_at`(`deleted_at` ASC, `created_at` DESC) USING BTREE,
  CONSTRAINT `movie_comment_ibfk_3` FOREIGN KEY (`movie_id`) REFERENCES `movie` (`movie_id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `movie_comment_ibfk_4` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE = InnoDB AUTO_INCREMENT = 1942011793 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '评论表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for movie_language
-- ----------------------------
DROP TABLE IF EXISTS `movie_language`;
CREATE TABLE `movie_language`  (
  `movie_id` bigint NOT NULL,
  `lang_id` int NOT NULL,
  `is_primary` tinyint(1) NULL DEFAULT 0 COMMENT '是否为原声/主语言',
  PRIMARY KEY (`movie_id`, `lang_id`) USING BTREE,
  INDEX `idx_lang_id`(`lang_id` ASC) USING BTREE,
  CONSTRAINT `fk_mlr_lang` FOREIGN KEY (`lang_id`) REFERENCES `dict_language` (`lang_id`) ON DELETE RESTRICT ON UPDATE RESTRICT,
  CONSTRAINT `fk_mlr_movie` FOREIGN KEY (`movie_id`) REFERENCES `movie` (`movie_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '电影语言关联表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for movie_person
-- ----------------------------
DROP TABLE IF EXISTS `movie_person`;
CREATE TABLE `movie_person`  (
  `movie_person_id` bigint NOT NULL AUTO_INCREMENT,
  `movie_id` bigint NOT NULL,
  `person_id` bigint NOT NULL,
  `person_role` enum('director','actor','writer','producer') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '职责',
  `character_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '饰演角色名',
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`movie_person_id`) USING BTREE,
  INDEX `idx_movie_id`(`movie_id` ASC) USING BTREE,
  INDEX `idx_person_role`(`person_role` ASC) USING BTREE,
  INDEX `idx_person_id`(`person_id` ASC) USING BTREE,
  CONSTRAINT `movie_person_ibfk_1` FOREIGN KEY (`movie_id`) REFERENCES `movie` (`movie_id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `movie_person_ibfk_2` FOREIGN KEY (`person_id`) REFERENCES `person` (`person_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB AUTO_INCREMENT = 249753 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '影视人员表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for movie_region
-- ----------------------------
DROP TABLE IF EXISTS `movie_region`;
CREATE TABLE `movie_region`  (
  `movie_id` bigint NOT NULL,
  `region_id` int NOT NULL,
  PRIMARY KEY (`movie_id`, `region_id`) USING BTREE,
  INDEX `idx_region_id`(`region_id` ASC) USING BTREE,
  CONSTRAINT `fk_mrr_movie` FOREIGN KEY (`movie_id`) REFERENCES `movie` (`movie_id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `fk_mrr_region` FOREIGN KEY (`region_id`) REFERENCES `dict_region` (`region_id`) ON DELETE RESTRICT ON UPDATE RESTRICT
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '电影地区关联表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for movie_tag
-- ----------------------------
DROP TABLE IF EXISTS `movie_tag`;
CREATE TABLE `movie_tag`  (
  `movie_id` bigint NOT NULL,
  `tag_id` bigint NOT NULL COMMENT '标签ID',
  `weight` decimal(10, 4) NULL DEFAULT 1.0000 COMMENT '标签权重',
  `vote_up` int NULL DEFAULT 0 COMMENT '赞同数',
  `hot_score` decimal(10, 4) NULL DEFAULT 0.0000 COMMENT '热度分',
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`movie_id`, `tag_id`) USING BTREE,
  INDEX `idx_movie_id`(`movie_id` ASC) USING BTREE,
  INDEX `idx_tag_id`(`tag_id` ASC) USING BTREE,
  INDEX `idx_weight`(`weight` ASC) USING BTREE,
  CONSTRAINT `movie_tag_ibfk_3` FOREIGN KEY (`movie_id`) REFERENCES `movie` (`movie_id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `movie_tag_ibfk_4` FOREIGN KEY (`tag_id`) REFERENCES `tag_dict` (`tag_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '影视动态标签关联表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for notification
-- ----------------------------
DROP TABLE IF EXISTS `notification`;
CREATE TABLE `notification`  (
  `noti_id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` bigint NOT NULL COMMENT '接收通知的用户',
  `sender_id` bigint NULL DEFAULT NULL COMMENT '发送者（系统消息为NULL）',
  `type` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '\'like\', \'reply\', \'follower\', \'sys\'...',
  `content` json NULL,
  `is_readed` enum('true','false') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT 'false',
  `created_at` datetime NULL DEFAULT NULL,
  `updated_at` datetime NULL DEFAULT NULL,
  PRIMARY KEY (`noti_id`) USING BTREE,
  INDEX `updated_at`(`updated_at` DESC) USING BTREE,
  INDEX `sender_id`(`sender_id` ASC) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC, `is_readed` ASC) USING BTREE,
  CONSTRAINT `notification_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `notification_ibfk_2` FOREIGN KEY (`sender_id`) REFERENCES `user` (`user_id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE = InnoDB AUTO_INCREMENT = 2587 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '通知' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for person
-- ----------------------------
DROP TABLE IF EXISTS `person`;
CREATE TABLE `person`  (
  `person_id` bigint NOT NULL,
  `person_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '姓名',
  `photo` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '照片链接',
  `gender` enum('male','female','unknown') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT 'unknown' COMMENT '性别',
  `birth` date NULL DEFAULT NULL COMMENT '出生年月日',
  `bio` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '简介',
  PRIMARY KEY (`person_id`) USING BTREE,
  INDEX `idx_person_id`(`person_id` ASC) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '工作人员' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for rating
-- ----------------------------
DROP TABLE IF EXISTS `rating`;
CREATE TABLE `rating`  (
  `user_id` bigint NOT NULL COMMENT '评分用户',
  `movie_id` bigint NOT NULL COMMENT '评分对象',
  `rating` tinyint NOT NULL COMMENT '评分1-10',
  `updated_at` datetime NULL DEFAULT NULL COMMENT '评分时间',
  PRIMARY KEY (`user_id`, `movie_id`) USING BTREE,
  INDEX `movie_id`(`movie_id` ASC) USING BTREE,
  INDEX `user_id`(`user_id` ASC) USING BTREE,
  INDEX `updated_at`(`updated_at` DESC) USING BTREE,
  CONSTRAINT `chk_rating_range` CHECK ((`rating` >= 1) and (`rating` <= 10)),
  CONSTRAINT `rating_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `rating_ibfk_2` FOREIGN KEY (`movie_id`) REFERENCES `movie` (`movie_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '用户评分' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for rec_log
-- ----------------------------
DROP TABLE IF EXISTS `rec_log`;
CREATE TABLE `rec_log`  (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` bigint NULL DEFAULT NULL COMMENT '用户ID(可为空,游客)',
  `rec_type` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '推荐类型',
  `request_params` json NULL COMMENT '推荐时的上下文',
  `response_time` bigint NULL DEFAULT NULL COMMENT '响应延时(ms)',
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP COMMENT '推荐时间',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 62 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '推荐请求日志表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for tag_dict
-- ----------------------------
DROP TABLE IF EXISTS `tag_dict`;
CREATE TABLE `tag_dict`  (
  `tag_id` bigint NOT NULL AUTO_INCREMENT,
  `tag_name` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '标签名',
  `user_id` bigint NULL DEFAULT NULL COMMENT '创建者ID',
  `collect_count` int NULL DEFAULT NULL COMMENT '收藏该标签的人数',
  `movie_count` int NULL DEFAULT NULL COMMENT '关联的电影数（多少电影有此标签）',
  `type` enum('static','dynamic') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT 'dynamic' COMMENT '动态/静态标签',
  `status` enum('hide','show') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT 'show' COMMENT '状态：是否展示',
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`tag_id`) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE,
  INDEX `tag_name`(`tag_name` ASC) USING BTREE,
  CONSTRAINT `tag_dict_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE SET NULL ON UPDATE CASCADE
) ENGINE = InnoDB AUTO_INCREMENT = 203198 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '动态标签字典表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for tag_vote
-- ----------------------------
DROP TABLE IF EXISTS `tag_vote`;
CREATE TABLE `tag_vote`  (
  `user_id` bigint NOT NULL,
  `movie_id` bigint NOT NULL,
  `tag_id` bigint NOT NULL,
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '赞同时间',
  PRIMARY KEY (`user_id`, `movie_id`, `tag_id`) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE,
  INDEX `idx_movie_id`(`movie_id` ASC) USING BTREE,
  INDEX `idx_tag_id`(`tag_id` ASC) USING BTREE,
  CONSTRAINT `tag_vote_ibfk_1` FOREIGN KEY (`tag_id`) REFERENCES `tag_dict` (`tag_id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `tag_vote_ibfk_2` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `tag_vote_ibfk_3` FOREIGN KEY (`movie_id`) REFERENCES `movie` (`movie_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '标签投票表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for user
-- ----------------------------
DROP TABLE IF EXISTS `user`;
CREATE TABLE `user`  (
  `user_id` bigint NOT NULL AUTO_INCREMENT,
  `password_hash` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL COMMENT '密码哈希',
  `email` varchar(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '邮箱（登录注册区分账号的标准）',
  `status` enum('active','banned','deleted') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT 'active' COMMENT '账号状态',
  `role` enum('user','admin','super_admin') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT 'user' COMMENT '用户角色',
  `nickname` varchar(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '昵称',
  `phone` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '手机号',
  `avatar` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '头像URL',
  `birth` date NULL DEFAULT NULL COMMENT '出生日期',
  `gender` enum('male','female','unknown') CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT 'unknown' COMMENT '性别',
  `bio` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '简介',
  `profession` varchar(20) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NULL DEFAULT NULL COMMENT '职业',
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `deleted_at` datetime NULL DEFAULT NULL,
  PRIMARY KEY (`user_id`) USING BTREE,
  UNIQUE INDEX `email`(`email` ASC) USING BTREE,
  INDEX `idx_email`(`email` ASC) USING BTREE,
  INDEX `idx_status`(`status` ASC) USING BTREE
) ENGINE = InnoDB AUTO_INCREMENT = 649125 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '用户账号表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for user_click
-- ----------------------------
DROP TABLE IF EXISTS `user_click`;
CREATE TABLE `user_click`  (
  `id` bigint NOT NULL AUTO_INCREMENT COMMENT '主键ID',
  `user_id` bigint NOT NULL COMMENT '用户ID',
  `movie_id` bigint NOT NULL COMMENT '电影ID',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '点击时间',
  PRIMARY KEY (`id`) USING BTREE,
  INDEX `idx_user_time`(`user_id` ASC, `created_at` DESC) USING BTREE,
  INDEX `idx_movie_time`(`movie_id` ASC, `created_at` DESC) USING BTREE
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_0900_ai_ci ROW_FORMAT = Dynamic COMMENT = '用户点击流水表';

-- ----------------------------
-- Table structure for user_collect_movie
-- ----------------------------
DROP TABLE IF EXISTS `user_collect_movie`;
CREATE TABLE `user_collect_movie`  (
  `user_id` bigint NOT NULL,
  `movie_id` bigint NOT NULL,
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`, `movie_id`) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE,
  INDEX `movie_id`(`movie_id` ASC) USING BTREE,
  INDEX `created_at`(`created_at` DESC) USING BTREE,
  CONSTRAINT `user_collect_movie_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `user_collect_movie_ibfk_2` FOREIGN KEY (`movie_id`) REFERENCES `movie` (`movie_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '用户收藏表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for user_collect_tag
-- ----------------------------
DROP TABLE IF EXISTS `user_collect_tag`;
CREATE TABLE `user_collect_tag`  (
  `user_id` bigint NOT NULL,
  `tag_id` bigint NOT NULL,
  `is_static` tinyint(1) NULL DEFAULT 1 COMMENT '是否静态标签',
  `created_at` datetime NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`user_id`, `tag_id`) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE,
  INDEX `tag_id`(`tag_id` ASC) USING BTREE,
  CONSTRAINT `user_collect_tag_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `user_collect_tag_ibfk_2` FOREIGN KEY (`tag_id`) REFERENCES `tag_dict` (`tag_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '用户兴趣标签表' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for user_follow
-- ----------------------------
DROP TABLE IF EXISTS `user_follow`;
CREATE TABLE `user_follow`  (
  `user_id` bigint NOT NULL COMMENT '关注发起人',
  `follow_id` bigint NOT NULL COMMENT '被关注者',
  `created_at` datetime NULL DEFAULT NULL COMMENT '关注时间',
  PRIMARY KEY (`user_id`, `follow_id`) USING BTREE,
  INDEX `user_follow_ibfk_2`(`follow_id` ASC) USING BTREE,
  INDEX `user_id`(`user_id` ASC) USING BTREE,
  CONSTRAINT `user_follow_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE RESTRICT,
  CONSTRAINT `user_follow_ibfk_2` FOREIGN KEY (`follow_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '用户关注' ROW_FORMAT = DYNAMIC;

-- ----------------------------
-- Table structure for user_token
-- ----------------------------
DROP TABLE IF EXISTS `user_token`;
CREATE TABLE `user_token`  (
  `token_id` bigint NOT NULL AUTO_INCREMENT,
  `user_id` bigint NOT NULL,
  `token` varchar(500) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `expires_at` datetime NOT NULL COMMENT '过期时间',
  `created_at` datetime NULL DEFAULT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`token_id`) USING BTREE,
  UNIQUE INDEX `idx_token`(`token` ASC) USING BTREE,
  INDEX `idx_user_id`(`user_id` ASC) USING BTREE,
  CONSTRAINT `user_token_ibfk_1` FOREIGN KEY (`user_id`) REFERENCES `user` (`user_id`) ON DELETE CASCADE ON UPDATE RESTRICT
) ENGINE = InnoDB AUTO_INCREMENT = 126 CHARACTER SET = utf8mb4 COLLATE = utf8mb4_unicode_ci COMMENT = '用户Token表' ROW_FORMAT = DYNAMIC;

SET FOREIGN_KEY_CHECKS = 1;

-- ----------------------------
-- Table structure for movie_embeddings
-- ----------------------------
DROP TABLE IF EXISTS `movie_embeddings`;
CREATE TABLE `movie_embeddings` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'primary id',
  `movie_id` BIGINT UNSIGNED NOT NULL COMMENT 'business movie id',
  `chunk_text` TEXT NOT NULL COMMENT 'source text for embedding',
  `embedding_vector` BLOB NOT NULL COMMENT 'serialized float32 vector',
  `created_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_movie_id` (`movie_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='movie embedding cold storage';
