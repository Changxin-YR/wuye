"""Presentation metadata only. All authorization and writes remain server-side."""
from models import *
MODULE_CONFIG={
 'communities':(Community,'小区','property.read','name address phone','community.save'),
 'buildings':(Building,'楼栋','property.read','community_id name floors','building.save'),
 'units':(PropertyUnit,'单元','property.read','building_id name','unit.save'),
 'houses':(House,'房屋','property.read','building_name unit room_no area usage occupancy ownership','house.save'),
 'people':(Person,'人员档案','person.read','name phone community_id user_id','person.save'),
 'relations':(HousePerson,'房屋人员关系','property.read','house_id person_id kind start_at end_at status','relation.bind'),
 'leases':(Lease,'租户与租赁','property.read','house_id start_date end_date move_in move_out status','lease.create'),
 'staff':(User,'账号与岗位','staff.manage','username real_name phone active','staff.create'),
 'roles':(RbacRole,'角色权限','rbac.manage','code name builtin','role.save'),
 'work-orders':(WorkOrder,'维修工单','order.read','order_no title location status repairer_id created_at','order.create'),
 'complaints':(Complaint,'投诉建议','complaint.read','title house_id status assignee_id created_at','complaint.create'),
 'notices':(Notice,'公告通知','notice.read','title community_id building_id created_at','notice.save'),
 'visitors':(Visitor,'访客登记','visitor.read','name house_id phone purpose status expected_at','visitor.create'),
 'vehicles':(Vehicle,'车辆','vehicle.read','plate house_id person_id model status','vehicle.save'),
 'parking':(ParkingSpace,'车位','parking.read','code location status','parking.save'),
 'parking-uses':(ParkingUse,'车位使用记录','parking.read','space_id vehicle_id start_at end_at status','parking.assign'),
 'devices':(Device,'设备设施','device.read','code name location category status','device.save'),
 'inspections':(Inspection,'巡检任务','inspection.read','device_id assignee_id due_at status completed_at','inspection.create'),
 'fees':(FeeItem,'收费项目','billing.read','name community_id basis rate','fee.save'),
 'bills':(Bill,'应收账单','billing.read','title house_id amount_cents paid_cents status due_date','bill.create'),
 'payments':(Payment,'收款与冲销','billing.read','bill_id amount_cents channel reference status created_at','payment.record'),
 'audit':(AuditLog,'操作审计','audit.read','actor_name roles source action target status created_at trace_id',None),
}
LABELS=dict(x.split(':',1) for x in '''id:记录编号 version:记录版本 name:名称 address:地址 phone:联系电话 community_id:小区 building_id:楼栋 unit_id:单元 building_name:楼栋名称 unit:单元 room_no:房号 floors:楼层数 area:建筑面积（㎡） usage:用途 occupancy:入住状态 ownership:产权状态 kind:关系类型 person_id:人员 person_name:人员姓名 house_id:房屋 user_id:关联住户账号 emergency_contact:紧急联系人 note:备注 is_resident:实际居住 person_ids:共同入住人员 start_date:租期开始 end_date:租期结束 move_in:入住时间 move_out:退租时间 start_at:生效时间 end_at:失效时间 reason:核验原因 status:状态 username:登录账号 password:初始密码 real_name:姓名 role_codes:岗位 scope_kind:数据范围 auth_version:账号版本 active:启用 code:编码 permissions:权限 title:标题 content:详细内容 requester_person_id:报修住户 type:报修分类 location:具体位置 contact_name:联系人 contact_phone:联系电话 repairer_id:维修人员 remark:处理说明 score:评分 comment:评价 order_no:工单号 category:分类 assignee_id:负责人 resolution:处理结果 host_person_id:被访住户 purpose:来访事由 expected_at:预计来访时间 check_in:进入时间 check_out:离开时间 plate:车牌 model:车型 space_id:车位 vehicle_id:车辆 device_id:设备 due_at:计划完成时间 completed_at:实际完成时间 checklist:检查项目 findings:检查结果 fault:发现故障并创建报修 work_order_id:关联报修 fee_item_id:收费项目 basis:计费方式 rate:单价（元） period:账期 due_date:缴费截止 amount:本次收款（元） amount_cents:应收/收款金额 paid_cents:已收金额 bill_id:账单 channel:收款渠道 reference:收据号或银行流水 environment:运行环境 reversed_at:冲销时间 calculation:计费快照 void_reason:作废原因 created_at:创建时间 updated_at:更新时间 created_by:创建人 updated_by:操作人 deleted:已归档 actor_name:操作账号 roles:操作岗位 source:操作来源 action:操作类型 target:目标编号 resource:目标类型 before_data:修改前 after_data:修改后 error:失败原因 trace_id:追踪编号 operator_id:操作人 builtin:内置岗位'''.split())
REFS={'community_id':'communities','building_id':'buildings','unit_id':'units','house_id':'houses','person_id':'people','person_ids':'people','host_person_id':'people','requester_person_id':'people','user_id':'staff','repairer_id':'staff','assignee_id':'staff','vehicle_id':'vehicles','space_id':'parking','device_id':'devices','bill_id':'bills','fee_item_id':'fees'}
ENUMS={
 'usage':{'residential':'住宅','commercial':'商铺','office':'办公'},'occupancy':{'vacant':'空置','owner_occupied':'业主自住','renovating':'装修'},'ownership':{'private':'私有产权','public':'公共产权','unknown':'待核验'},'kind':{'owner':'产权人','family':'家庭成员','contact':'联系人'},'scope_kind':{'community':'负责小区','building':'负责楼栋','assigned':'本人任务','self':'住户本人','all':'全域（仅超级管理员）'},'basis':{'area':'建筑面积 × 单价','fixed':'每户固定金额'},'channel':{'cash':'已核验现金收款','bank':'已核验银行收款','mock':'测试支付（禁止生产使用）'},'type':{x:x for x in ['水电故障','门窗维修','公共设施','其他']},'active':{'1':'启用','0':'停用'},'is_resident':{'1':'是','0':'否'},'fault':{'0':'未发现故障','1':'发现故障并自动报修'},'status':{'normal':'正常','fault':'故障','maintenance':'检修','retired':'停用'}}
VALUES={'vacant':'空置','owner_occupied':'业主自住','rented':'出租','renovating':'装修','private':'私有','public':'公共','unknown':'待核验','residential':'住宅','commercial':'商铺','office':'办公','owner':'产权人','family':'家庭成员','tenant':'租户','contact':'联系人','active':'有效','ended':'已结束','open':'待处理','assigned':'已分配','resolved':'已处理待回访','closed':'已结案','registered':'已登记','inside':'已进入','left':'已离开','cancelled':'已取消','available':'可分配','occupied':'使用中','normal':'正常','fault':'故障','maintenance':'检修','retired':'停用','pending':'待完成','completed':'已完成','fixed':'固定金额','area':'按面积','unpaid':'未缴','partial':'部分缴费','paid':'已缴清','void':'已作废','posted':'有效收款','reversed':'已冲销','manual':'页面/API','agent':'AI Agent','cash':'现金','bank':'银行','mock':'测试支付','success':'成功','failure':'失败','production':'生产','test':'测试','dev':'开发'}
ACTION_NAMES=dict(x.split(':',1) for x in '''community.save:保存小区 building.save:保存楼栋 unit.save:保存单元 house.save:保存房屋 house.ownership:核验产权状态 property.archive:归档房产 person.save:保存人员 person.archive:归档人员 relation.bind:绑定房屋人员 relation.bind_by_name:按姓名绑定业主 relation.end:解除房屋关系 lease.create:登记租户入住 lease.checkout:办理退租 staff.create:新增账号 staff.roles:修改岗位与数据范围 staff.state:启用或停用账号 role.save:保存自定义岗位 order.create:创建报修 order.assign:分配维修 order.reassign:改派维修 order.accept:接单 order.progress:登记进度 order.finish:提交完工 order.close:验收关闭 order.reopen:要求返修 order.cancel:取消报修 order.evaluate:评价工单 complaint.create:登记投诉 complaint.assign:分配处理人 complaint.resolve:提交处理结果 complaint.close:回访结案 notice.save:保存公告 notice.archive:撤下公告 visitor.create:登记访客 visitor.checkin:确认进入 visitor.checkout:确认离开 visitor.cancel:取消来访 vehicle.save:保存车辆 vehicle.archive:归档车辆 parking.save:保存车位 parking.assign:分配车位 parking.release:结束车位使用 device.save:保存设备 device.archive:归档设备 inspection.create:分配巡检 inspection.complete:提交巡检结果 fee.save:保存收费项目 bill.create:生成账单 bill.void:作废账单 payment.record:登记已核验收款 payment.reverse:冲销收款记录'''.split())
ACTIONS={
 'communities':['community.save','property.archive'],'buildings':['building.save','property.archive'],'units':['unit.save','property.archive'],
 'houses':['house.save','house.ownership','relation.bind','lease.create','order.create','bill.create','property.archive'],
 'people':['person.save','person.archive'],'relations':['relation.end'],'leases':['lease.checkout'],
 'staff':['staff.roles','staff.state'],'roles':[],
 'work-orders':['order.assign','order.reassign','order.accept','order.progress','order.finish','order.close','order.reopen','order.cancel','order.evaluate'],
 'complaints':['complaint.assign','complaint.resolve','complaint.close'],'notices':['notice.save','notice.batch_publish','notice.archive'],
 'visitors':['visitor.checkin','visitor.checkout','visitor.cancel'],'vehicles':['vehicle.save','vehicle.archive'],
 'parking':['parking.save','parking.assign'],'parking-uses':['parking.release'],'devices':['device.save','device.archive','inspection.create'],'inspections':['inspection.complete'],
 'fees':['fee.save'],'bills':['payment.record','bill.void'],'payments':['payment.reverse'],'audit':[]}

ACTION_NAMES['bill.batch']='按楼栋批量生成月账单'
ACTION_NAMES['notice.batch_publish']='向所有负责小区发布公告'
