-- Generated from models.py; fresh MySQL database only.

-- Existing databases: python manage.py upgrade-db

CREATE DATABASE IF NOT EXISTS property_workorder CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE property_workorder;

SET NAMES utf8mb4;


CREATE TABLE rbac_role (
	code VARCHAR(40) NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	builtin BOOL NOT NULL, 
	PRIMARY KEY (code)
);


CREATE TABLE schema_migration (
	revision VARCHAR(30) NOT NULL, 
	applied_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (revision)
);


CREATE TABLE sys_user (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	username VARCHAR(50) NOT NULL, 
	password_hash VARCHAR(255) NOT NULL, 
	real_name VARCHAR(50) NOT NULL, 
	phone VARCHAR(20) NOT NULL, 
	`role` INTEGER NOT NULL, 
	avatar VARCHAR(255) NOT NULL, 
	active BOOL NOT NULL DEFAULT '1', 
	auth_version INTEGER NOT NULL DEFAULT '1', 
	failed_logins INTEGER NOT NULL DEFAULT '0', 
	locked_until DATETIME(6), 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_user_role CHECK (role IN (0,1,2)), 
	UNIQUE (username)
);


CREATE TABLE system_setting (
	`key` VARCHAR(50) NOT NULL, 
	value VARCHAR(200) NOT NULL, 
	PRIMARY KEY (`key`)
);


CREATE TABLE ai_conversation (
	id VARCHAR(36) NOT NULL, 
	user_id INTEGER NOT NULL, 
	upstream_id VARCHAR(128) NOT NULL, 
	scope_hash VARCHAR(64) NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES sys_user (id)
);

CREATE INDEX ix_ai_conversation_user_id ON ai_conversation (user_id);


CREATE TABLE ai_grant (
	id VARCHAR(36) NOT NULL, 
	token_hash VARCHAR(64) NOT NULL, 
	user_id INTEGER NOT NULL, 
	auth_version INTEGER NOT NULL, 
	expires_at DATETIME(6) NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (token_hash), 
	FOREIGN KEY(user_id) REFERENCES sys_user (id)
);

CREATE INDEX ix_ai_grant_user_id ON ai_grant (user_id);


CREATE TABLE business_request (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	request_key VARCHAR(100) NOT NULL, 
	digest VARCHAR(64) NOT NULL, 
	response TEXT NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uk_business_request UNIQUE (user_id, request_key), 
	FOREIGN KEY(user_id) REFERENCES sys_user (id)
);


CREATE TABLE community (
	name VARCHAR(80) NOT NULL, 
	address VARCHAR(200) NOT NULL, 
	phone VARCHAR(20) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	PRIMARY KEY (id), 
	UNIQUE (name), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id)
);


CREATE TABLE role_permission (
	role_code VARCHAR(40) NOT NULL, 
	permission VARCHAR(60) NOT NULL, 
	PRIMARY KEY (role_code, permission), 
	FOREIGN KEY(role_code) REFERENCES rbac_role (code)
);


CREATE TABLE user_role (
	user_id INTEGER NOT NULL, 
	role_code VARCHAR(40) NOT NULL, 
	PRIMARY KEY (user_id, role_code), 
	FOREIGN KEY(user_id) REFERENCES sys_user (id), 
	FOREIGN KEY(role_code) REFERENCES rbac_role (code)
);


CREATE TABLE ai_action (
	id VARCHAR(36) NOT NULL, 
	grant_id VARCHAR(36) NOT NULL, 
	user_id INTEGER NOT NULL, 
	auth_version INTEGER NOT NULL, 
	command VARCHAR(40) NOT NULL, 
	payload TEXT NOT NULL, 
	payload_hash VARCHAR(64) NOT NULL, 
	preview TEXT NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	result TEXT NOT NULL, 
	expires_at DATETIME(6) NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	version INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uk_ai_proposal UNIQUE (grant_id, payload_hash), 
	FOREIGN KEY(grant_id) REFERENCES ai_grant (id), 
	FOREIGN KEY(user_id) REFERENCES sys_user (id)
);

CREATE INDEX ix_ai_action_user_id ON ai_action (user_id);


CREATE TABLE building (
	community_id INTEGER NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	floors INTEGER NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	PRIMARY KEY (id), 
	CONSTRAINT uk_building_name UNIQUE (community_id, name), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id)
);

CREATE INDEX ix_building_community_id ON building (community_id);


CREATE TABLE fee_item (
	community_id INTEGER NOT NULL, 
	name VARCHAR(80) NOT NULL, 
	basis VARCHAR(20) NOT NULL, 
	rate NUMERIC(12, 4) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	PRIMARY KEY (id), 
	CONSTRAINT uk_fee_item UNIQUE (community_id, name), 
	CONSTRAINT ck_fee_rate CHECK (rate > 0), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id)
);


CREATE TABLE person (
	community_id INTEGER NOT NULL, 
	user_id INTEGER, 
	name VARCHAR(50) NOT NULL, 
	phone VARCHAR(20) NOT NULL, 
	emergency_contact VARCHAR(100) NOT NULL, 
	note VARCHAR(500) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	PRIMARY KEY (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	UNIQUE (user_id), 
	FOREIGN KEY(user_id) REFERENCES sys_user (id), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id)
);

CREATE INDEX ix_person_community_id ON person (community_id);

CREATE INDEX ix_person_name ON person (name);

CREATE INDEX ix_person_phone ON person (phone);


CREATE TABLE audit_log (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	operator_id INTEGER NOT NULL, 
	actor_name VARCHAR(50) NOT NULL DEFAULT '', 
	roles VARCHAR(500) NOT NULL DEFAULT '', 
	source VARCHAR(20) NOT NULL DEFAULT 'manual', 
	resource VARCHAR(50) NOT NULL DEFAULT '', 
	before_data TEXT, 
	after_data TEXT, 
	status VARCHAR(20) NOT NULL DEFAULT 'success', 
	error VARCHAR(500) NOT NULL DEFAULT '', 
	trace_id VARCHAR(64), 
	community_id INTEGER, 
	building_id INTEGER, 
	action VARCHAR(50) NOT NULL, 
	target VARCHAR(100) NOT NULL, 
	detail VARCHAR(500) NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(operator_id) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id)
);

CREATE INDEX ix_audit_log_trace_id ON audit_log (trace_id);


CREATE TABLE device (
	code VARCHAR(50) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	category VARCHAR(30) NOT NULL, 
	location VARCHAR(200) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT uk_device_code UNIQUE (community_id, code), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_device_state CHECK (status IN ('normal','fault','maintenance','retired'))
);

CREATE INDEX ix_device_building_id ON device (building_id);

CREATE INDEX ix_device_community_id ON device (community_id);


CREATE TABLE notice (
	community_id INTEGER NOT NULL DEFAULT '1', 
	building_id INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	title VARCHAR(100) NOT NULL, 
	content TEXT NOT NULL, 
	version INTEGER NOT NULL DEFAULT '1', 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id)
);


CREATE TABLE parking_space (
	code VARCHAR(50) NOT NULL, 
	location VARCHAR(200) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT uk_parking_code UNIQUE (community_id, code), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_parking_space_state CHECK (status IN ('available','occupied'))
);

CREATE INDEX ix_parking_space_building_id ON parking_space (building_id);

CREATE INDEX ix_parking_space_community_id ON parking_space (community_id);


CREATE TABLE property_unit (
	name VARCHAR(20) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT uk_unit_name UNIQUE (building_id, name), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id)
);

CREATE INDEX ix_property_unit_building_id ON property_unit (building_id);

CREATE INDEX ix_property_unit_community_id ON property_unit (community_id);


CREATE TABLE user_scope (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	community_id INTEGER, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_scope_kind CHECK (kind IN ('all','community','building','assigned','self'))
);

CREATE INDEX ix_user_scope_user_id ON user_scope (user_id);


CREATE TABLE house (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	community_id INTEGER NOT NULL DEFAULT '1', 
	building_id INTEGER, 
	unit_id INTEGER, 
	area NUMERIC(10, 2) NOT NULL DEFAULT '0', 
	`usage` VARCHAR(20) NOT NULL DEFAULT 'residential', 
	occupancy VARCHAR(20) NOT NULL DEFAULT 'vacant', 
	ownership VARCHAR(20) NOT NULL DEFAULT 'private', 
	deleted BOOL NOT NULL DEFAULT '0', 
	building_name VARCHAR(50) NOT NULL, 
	unit VARCHAR(20) NOT NULL, 
	room_no INTEGER NOT NULL, 
	owner_id INTEGER, 
	version INTEGER NOT NULL DEFAULT '1', 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uk_house_unit_room UNIQUE (unit_id, room_no), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	FOREIGN KEY(unit_id) REFERENCES property_unit (id), 
	FOREIGN KEY(owner_id) REFERENCES sys_user (id) ON DELETE SET NULL
);


CREATE TABLE bill (
	house_id INTEGER NOT NULL, 
	fee_item_id INTEGER NOT NULL, 
	title VARCHAR(100) NOT NULL, 
	period VARCHAR(7) NOT NULL, 
	due_date DATE NOT NULL, 
	amount_cents INTEGER NOT NULL, 
	paid_cents INTEGER NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	calculation VARCHAR(500) NOT NULL, 
	void_reason VARCHAR(300) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT uk_monthly_bill UNIQUE (house_id, fee_item_id, period), 
	CONSTRAINT ck_bill_amounts CHECK (amount_cents > 0 AND paid_cents >= 0 AND paid_cents <= amount_cents), 
	FOREIGN KEY(house_id) REFERENCES house (id), 
	FOREIGN KEY(fee_item_id) REFERENCES fee_item (id), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_bill_state CHECK (status IN ('unpaid','partial','paid','void'))
);

CREATE INDEX ix_bill_building_id ON bill (building_id);

CREATE INDEX ix_bill_community_id ON bill (community_id);


CREATE TABLE complaint (
	house_id INTEGER NOT NULL, 
	reporter_id INTEGER NOT NULL, 
	assignee_id INTEGER, 
	title VARCHAR(100) NOT NULL, 
	content TEXT NOT NULL, 
	category VARCHAR(30) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	resolution VARCHAR(1000) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(house_id) REFERENCES house (id), 
	FOREIGN KEY(reporter_id) REFERENCES sys_user (id), 
	FOREIGN KEY(assignee_id) REFERENCES sys_user (id), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_complaint_state CHECK (status IN ('open','assigned','resolved','closed'))
);

CREATE INDEX ix_complaint_building_id ON complaint (building_id);

CREATE INDEX ix_complaint_community_id ON complaint (community_id);


CREATE TABLE lease (
	house_id INTEGER NOT NULL, 
	start_date DATE NOT NULL, 
	end_date DATE NOT NULL, 
	move_in DATETIME(6) NOT NULL, 
	move_out DATETIME(6), 
	status VARCHAR(20) NOT NULL, 
	active_key INTEGER, 
	note VARCHAR(500) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_lease_dates CHECK (end_date >= start_date), 
	FOREIGN KEY(house_id) REFERENCES house (id), 
	UNIQUE (active_key), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_lease_state CHECK (status IN ('active','ended'))
);

CREATE INDEX ix_lease_building_id ON lease (building_id);

CREATE INDEX ix_lease_community_id ON lease (community_id);

CREATE INDEX ix_lease_house_id ON lease (house_id);


CREATE TABLE vehicle (
	house_id INTEGER NOT NULL, 
	person_id INTEGER NOT NULL, 
	plate VARCHAR(20) NOT NULL, 
	model VARCHAR(50) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT uk_vehicle_plate UNIQUE (community_id, plate), 
	FOREIGN KEY(house_id) REFERENCES house (id), 
	FOREIGN KEY(person_id) REFERENCES person (id), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_vehicle_state CHECK (status IN ('active','archived'))
);

CREATE INDEX ix_vehicle_building_id ON vehicle (building_id);

CREATE INDEX ix_vehicle_community_id ON vehicle (community_id);


CREATE TABLE visitor (
	house_id INTEGER NOT NULL, 
	host_person_id INTEGER NOT NULL, 
	name VARCHAR(50) NOT NULL, 
	phone VARCHAR(20) NOT NULL, 
	purpose VARCHAR(200) NOT NULL, 
	expected_at DATETIME(6) NOT NULL, 
	check_in DATETIME(6), 
	check_out DATETIME(6), 
	status VARCHAR(20) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(house_id) REFERENCES house (id), 
	FOREIGN KEY(host_person_id) REFERENCES person (id), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_visitor_state CHECK (status IN ('registered','inside','left','cancelled'))
);

CREATE INDEX ix_visitor_building_id ON visitor (building_id);

CREATE INDEX ix_visitor_community_id ON visitor (community_id);


CREATE TABLE work_order (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	community_id INTEGER NOT NULL DEFAULT '1', 
	building_id INTEGER, 
	requester_person_id INTEGER, 
	order_no VARCHAR(64) NOT NULL, 
	owner_id INTEGER NOT NULL, 
	repairer_id INTEGER, 
	house_id INTEGER, 
	title VARCHAR(100) NOT NULL, 
	content TEXT NOT NULL, 
	img_url VARCHAR(500) NOT NULL, 
	type VARCHAR(30) NOT NULL, 
	status INTEGER NOT NULL, 
	location VARCHAR(200) NOT NULL DEFAULT '', 
	contact_name VARCHAR(50) NOT NULL DEFAULT '', 
	contact_phone VARCHAR(20) NOT NULL DEFAULT '', 
	resolution VARCHAR(1000) NOT NULL DEFAULT '', 
	version INTEGER NOT NULL DEFAULT '1', 
	accept_time DATETIME(6), 
	finish_time DATETIME(6), 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_order_status CHECK (status IN (0,1,2,3,4,5)), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	FOREIGN KEY(requester_person_id) REFERENCES person (id), 
	UNIQUE (order_no), 
	FOREIGN KEY(owner_id) REFERENCES sys_user (id), 
	FOREIGN KEY(repairer_id) REFERENCES sys_user (id), 
	FOREIGN KEY(house_id) REFERENCES house (id) ON DELETE SET NULL
);

CREATE INDEX idx_order_owner_status ON work_order (owner_id, status);

CREATE INDEX idx_order_repairer_status ON work_order (repairer_id, status);


CREATE TABLE house_person (
	house_id INTEGER NOT NULL, 
	person_id INTEGER NOT NULL, 
	lease_id INTEGER, 
	kind VARCHAR(20) NOT NULL, 
	is_resident BOOL NOT NULL, 
	start_at DATETIME(6) NOT NULL, 
	end_at DATETIME(6), 
	status VARCHAR(20) NOT NULL, 
	active_key VARCHAR(100), 
	reason VARCHAR(300) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_relation_kind CHECK (kind IN ('owner','family','tenant','contact')), 
	CONSTRAINT ck_relation_dates CHECK (end_at IS NULL OR end_at >= start_at), 
	FOREIGN KEY(house_id) REFERENCES house (id), 
	FOREIGN KEY(person_id) REFERENCES person (id), 
	FOREIGN KEY(lease_id) REFERENCES lease (id), 
	UNIQUE (active_key), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_house_person_state CHECK (status IN ('active','ended'))
);

CREATE INDEX idx_relation_house_person ON house_person (house_id, person_id, status);

CREATE INDEX ix_house_person_building_id ON house_person (building_id);

CREATE INDEX ix_house_person_community_id ON house_person (community_id);


CREATE TABLE inspection (
	device_id INTEGER NOT NULL, 
	assignee_id INTEGER NOT NULL, 
	due_at DATETIME(6) NOT NULL, 
	completed_at DATETIME(6), 
	checklist VARCHAR(1000) NOT NULL, 
	findings VARCHAR(1000) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	work_order_id INTEGER, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(device_id) REFERENCES device (id), 
	FOREIGN KEY(assignee_id) REFERENCES sys_user (id), 
	FOREIGN KEY(work_order_id) REFERENCES work_order (id), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_inspection_state CHECK (status IN ('pending','completed'))
);

CREATE INDEX ix_inspection_building_id ON inspection (building_id);

CREATE INDEX ix_inspection_community_id ON inspection (community_id);


CREATE TABLE notification (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	user_id INTEGER NOT NULL, 
	order_id INTEGER, 
	content VARCHAR(255) NOT NULL, 
	is_read BOOL NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES sys_user (id), 
	FOREIGN KEY(order_id) REFERENCES work_order (id)
);

CREATE INDEX ix_notification_user_id ON notification (user_id);


CREATE TABLE order_evaluate (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	order_id INTEGER NOT NULL, 
	score INTEGER NOT NULL, 
	comment VARCHAR(200) NOT NULL, 
	created_at DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_evaluate_score CHECK (score BETWEEN 1 AND 5), 
	UNIQUE (order_id), 
	FOREIGN KEY(order_id) REFERENCES work_order (id) ON DELETE CASCADE
);


CREATE TABLE order_log (
	id INTEGER NOT NULL AUTO_INCREMENT, 
	order_id INTEGER NOT NULL, 
	operator_id INTEGER, 
	before_status INTEGER, 
	after_status INTEGER, 
	remark VARCHAR(255) NOT NULL, 
	operate_time DATETIME(6) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(order_id) REFERENCES work_order (id) ON DELETE CASCADE, 
	FOREIGN KEY(operator_id) REFERENCES sys_user (id) ON DELETE SET NULL
);

CREATE INDEX idx_log_order_time ON order_log (order_id, operate_time);


CREATE TABLE parking_use (
	space_id INTEGER NOT NULL, 
	vehicle_id INTEGER NOT NULL, 
	start_at DATETIME(6) NOT NULL, 
	end_at DATETIME(6), 
	status VARCHAR(20) NOT NULL, 
	active_space INTEGER, 
	active_vehicle INTEGER, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(space_id) REFERENCES parking_space (id), 
	FOREIGN KEY(vehicle_id) REFERENCES vehicle (id), 
	UNIQUE (active_space), 
	UNIQUE (active_vehicle), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_parking_use_state CHECK (status IN ('active','ended'))
);

CREATE INDEX ix_parking_use_building_id ON parking_use (building_id);

CREATE INDEX ix_parking_use_community_id ON parking_use (community_id);


CREATE TABLE payment (
	house_id INTEGER NOT NULL, 
	bill_id INTEGER NOT NULL, 
	amount_cents INTEGER NOT NULL, 
	channel VARCHAR(20) NOT NULL, 
	reference VARCHAR(100) NOT NULL, 
	environment VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	reversed_at DATETIME(6), 
	reason VARCHAR(300) NOT NULL, 
	id INTEGER NOT NULL AUTO_INCREMENT, 
	created_at DATETIME(6) NOT NULL, 
	updated_at DATETIME(6) NOT NULL, 
	created_by INTEGER, 
	updated_by INTEGER, 
	deleted BOOL NOT NULL DEFAULT '0', 
	version INTEGER NOT NULL DEFAULT '1', 
	community_id INTEGER NOT NULL, 
	building_id INTEGER, 
	PRIMARY KEY (id), 
	CONSTRAINT uk_payment_receipt UNIQUE (community_id, channel, reference), 
	CONSTRAINT ck_payment_amount CHECK (amount_cents > 0), 
	FOREIGN KEY(house_id) REFERENCES house (id), 
	FOREIGN KEY(bill_id) REFERENCES bill (id), 
	FOREIGN KEY(created_by) REFERENCES sys_user (id), 
	FOREIGN KEY(updated_by) REFERENCES sys_user (id), 
	FOREIGN KEY(community_id) REFERENCES community (id), 
	FOREIGN KEY(building_id) REFERENCES building (id), 
	CONSTRAINT ck_payment_state CHECK (status IN ('posted','reversed'))
);

CREATE INDEX ix_payment_bill_id ON payment (bill_id);

CREATE INDEX ix_payment_building_id ON payment (building_id);

CREATE INDEX ix_payment_community_id ON payment (community_id);

INSERT IGNORE INTO community (id,name,address,phone,created_at,updated_at) VALUES (1,'待完善小区','','',UTC_TIMESTAMP(),UTC_TIMESTAMP());

INSERT IGNORE INTO rbac_role (code,name,builtin) VALUES ('superadmin','超级管理员',1);

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','audit.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','billing.collect');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','billing.manage');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','billing.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','billing.reverse');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','community.manage');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','complaint.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','complaint.handle');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','complaint.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','device.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','device.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','inspection.assign');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','inspection.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','inspection.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','lease.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','notice.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','notice.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','order.cancel');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','order.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','order.dispatch');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','order.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','order.verify');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','order.work');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','parking.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','parking.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','person.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','person.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','property.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','property.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','rbac.define');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','rbac.manage');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','relation.end');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','relation.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','staff.manage');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','vehicle.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','vehicle.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','visitor.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('superadmin','visitor.write');

INSERT IGNORE INTO rbac_role (code,name,builtin) VALUES ('manager','物业经理',1);

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','audit.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','billing.collect');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','billing.manage');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','billing.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','billing.reverse');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','community.manage');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','complaint.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','complaint.handle');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','complaint.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','device.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','device.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','inspection.assign');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','inspection.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','inspection.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','lease.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','notice.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','notice.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','order.cancel');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','order.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','order.dispatch');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','order.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','order.verify');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','order.work');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','parking.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','parking.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','person.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','person.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','property.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','property.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','rbac.manage');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','relation.end');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','relation.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','staff.manage');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','vehicle.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','vehicle.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','visitor.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('manager','visitor.write');

INSERT IGNORE INTO rbac_role (code,name,builtin) VALUES ('building_manager','楼栋管理员',1);

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','audit.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','billing.manage');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','billing.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','complaint.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','complaint.handle');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','complaint.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','device.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','device.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','inspection.assign');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','inspection.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','inspection.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','lease.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','notice.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','notice.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','order.cancel');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','order.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','order.dispatch');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','order.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','order.verify');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','order.work');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','parking.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','parking.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','person.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','person.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','property.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','property.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','relation.end');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','relation.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','vehicle.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','vehicle.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','visitor.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('building_manager','visitor.write');

INSERT IGNORE INTO rbac_role (code,name,builtin) VALUES ('customer_service','客服',1);

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','complaint.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','complaint.handle');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','complaint.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','lease.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','notice.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','order.cancel');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','order.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','order.dispatch');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','order.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','order.verify');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','person.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','person.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','property.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','relation.end');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','relation.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','visitor.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('customer_service','visitor.write');

INSERT IGNORE INTO rbac_role (code,name,builtin) VALUES ('frontdesk','前台',1);

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','complaint.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','complaint.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','lease.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','notice.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','order.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','order.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','person.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','person.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','property.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','relation.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','vehicle.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','visitor.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('frontdesk','visitor.write');

INSERT IGNORE INTO rbac_role (code,name,builtin) VALUES ('finance','财务',1);

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('finance','billing.collect');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('finance','billing.manage');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('finance','billing.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('finance','billing.reverse');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('finance','complaint.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('finance','complaint.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('finance','notice.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('finance','order.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('finance','order.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('finance','person.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('finance','property.read');

INSERT IGNORE INTO rbac_role (code,name,builtin) VALUES ('engineer','工程维修',1);

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('engineer','complaint.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('engineer','complaint.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('engineer','device.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('engineer','inspection.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('engineer','inspection.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('engineer','notice.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('engineer','order.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('engineer','order.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('engineer','order.work');

INSERT IGNORE INTO rbac_role (code,name,builtin) VALUES ('security','保安',1);

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','complaint.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','complaint.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','device.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','inspection.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','inspection.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','notice.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','order.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','order.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','parking.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','parking.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','person.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','property.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','vehicle.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','vehicle.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','visitor.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('security','visitor.write');

INSERT IGNORE INTO rbac_role (code,name,builtin) VALUES ('cleaner','保洁',1);

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('cleaner','complaint.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('cleaner','complaint.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('cleaner','device.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('cleaner','inspection.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('cleaner','inspection.write');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('cleaner','notice.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('cleaner','order.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('cleaner','order.read');

INSERT IGNORE INTO rbac_role (code,name,builtin) VALUES ('staff','普通工作人员',1);

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('staff','complaint.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('staff','complaint.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('staff','notice.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('staff','order.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('staff','order.read');

INSERT IGNORE INTO rbac_role (code,name,builtin) VALUES ('resident','住户',1);

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','billing.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','complaint.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','complaint.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','notice.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','order.cancel');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','order.create');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','order.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','order.verify');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','person.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','property.read');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','resident.self');

INSERT IGNORE INTO role_permission (role_code,permission) VALUES ('resident','vehicle.read');

INSERT IGNORE INTO schema_migration (revision,applied_at) VALUES ('property_v2',UTC_TIMESTAMP());

INSERT IGNORE INTO system_setting (`key`,value) VALUES ('runtime_environment','production');

