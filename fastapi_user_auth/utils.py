from functools import lru_cache
from typing import Any, Callable, Dict, List, Tuple, Type

from casbin import Enforcer
from fastapi_amis_admin.admin import FormAdmin, ModelAdmin, PageSchemaAdmin
from fastapi_amis_admin.admin.admin import AdminGroup, BaseActionAdmin, BaseAdminSite
from fastapi_amis_admin.utils.pydantic import model_fields
from pydantic import BaseModel

from fastapi_user_auth.auth.schemas import SystemUserEnum


def encode_admin_action(action: str) -> str:
    """将admin_action转换为casbin权限动作,以符合预期的匹配规则
    例如: 1.主体拥有admin:page:list权限,将同时拥有admin:page权限
    2.主体拥有admin:page:list:filter:name权限,将同时拥有admin:page:list:filter,admin:page:list,admin:page权限
    """
    perfix = "admin:"
    if action in {"page"}:  # admin:page
        return perfix + action
    perfix += "page:"
    if action in {"list", "update", "bulk_update", "create", "bulk_create", "read", "submit"}:
        return perfix + action  # admin:page:list;admin:page:submit
    perfix += "action:"
    return perfix + action  # admin:page:action:action_name


@lru_cache()
def get_admin_action_options(
    group: AdminGroup,
) -> List[Dict[str, Any]]:
    """获取全部页面权限,用于amis组件"""
    options = []
    for admin in group:  # 这里已经同步了数据库,所以只从这里配置权限就行了
        admin: PageSchemaAdmin
        if not admin.page_schema:
            continue

        item = {
            "label": admin.page_schema.label,
            "value": casbin_permission_encode(admin.unique_id, "admin:page"),
            "sort": admin.page_schema.sort,
        }
        if isinstance(admin, BaseActionAdmin):
            item["children"] = []
            if isinstance(admin, ModelAdmin):
                item["children"].append({"label": "查看列表", "value": casbin_permission_encode(admin.unique_id, "admin:list")})
            elif isinstance(admin, FormAdmin) and "submit" not in admin.registered_admin_actions:
                item["children"].append({"label": "提交", "value": casbin_permission_encode(admin.unique_id, "admin:submit")})
            for admin_action in admin.registered_admin_actions.values():
                item["children"].append(
                    {
                        "label": admin_action.label,
                        "value": casbin_permission_encode(admin.unique_id, f"admin:{admin_action.name}"),
                    }
                )
        elif isinstance(admin, AdminGroup):
            item["children"] = get_admin_action_options(admin)
        options.append(item)
    if options:
        options.sort(key=lambda p: p["sort"] or 0, reverse=True)
    return options


def filter_options(options: List[Dict[str, Any]], filter_func: Callable[[Dict[str, Any]], bool]) -> List[Dict[str, Any]]:
    """过滤选项,包含子选项.如果选项的children为空,则删除该选项"""
    result = []
    for option in options:
        if not filter_func(option):
            continue
        if option.get("children"):
            option["children"] = filter_options(option["children"], filter_func)
        result.append(option)
    return result


def admin_schema_fields_rows(
    admin: PageSchemaAdmin,
    schema: Type[BaseModel],
    action: str,
) -> List[Dict[str, Any]]:
    """解析schema字段,用于amis组件"""
    rows = []
    if not schema:
        return rows
    for field in model_fields(schema).values():
        label = field.field_info.title or field.name
        alias = field.alias or field.name
        label_prefix = {
            "list": "列表展示",
            "filter": "列表筛选",
            "update": "更新",
            "bulk_update": "批量更新",
            "create": "新增",
            "bulk_create": "批量新增",
            "read": "查看",
        }.get(action, "")
        label = f"{label_prefix}-{label}" if label_prefix else label
        rows.append(
            {
                "label": label,
                "rol": f"{action}:{alias}",
            }
        )
    return rows


def get_admin_action_fields_rows(
    admin: PageSchemaAdmin,
    action: str,
) -> List[Dict[str, Any]]:
    """获取指定页面权限的字段权限,用于amis组件"""
    rows = []
    if isinstance(admin, ModelAdmin):  # 模型管理
        if action in {"list"}:
            rows.extend(admin_schema_fields_rows(admin, admin.schema_list, "list"))  # 列表展示模型
            rows.extend(admin_schema_fields_rows(admin, admin.schema_filter, "filter"))  # 列表筛选模型
        elif action in {"update", "bulk_update"}:
            rows = admin_schema_fields_rows(admin, admin.schema_update, action)  # 更新模型
        elif action in {"create", "bulk_create"}:
            rows = admin_schema_fields_rows(admin, admin.schema_create, action)  # 创建模型
        elif action in {"read"}:
            rows = admin_schema_fields_rows(admin, admin.schema_read, action)  # 详情模型
    elif isinstance(admin, FormAdmin):  # 表单管理
        if action in {"submit"}:  # 表单提交模型
            rows = admin_schema_fields_rows(admin, admin.schema, action)
    return rows


async def get_admin_action_options_by_subject(
    enforcer: Enforcer,
    subject: str,
    group: AdminGroup,
):
    """获取指定subject主体的页面权限,用于amis组件"""
    # 获取全部页面权限
    options = get_admin_action_options(group)
    # 获取当前登录用户的权限
    if subject != "u:" + SystemUserEnum.ROOT:  # Root用户拥有全部权限
        permissions = await casbin_get_subject_permissions(enforcer, subject=subject, implicit=True)
        # 过滤掉没有权限的页面
        options = filter_options(options, filter_func=lambda item: item["value"] in permissions)
    return options


# 将casbin规则转化为字符串
def casbin_permission_encode(*field_values: str) -> str:
    """将casbin规则转化为字符串,从v1开始"""
    values = list(field_values)
    if len(values) < 5:
        values.extend([""] * (5 - len(values)))
    return "#".join(values)


# 将字符串转化为casbin规则
def casbin_permission_decode(permission: str) -> List[str]:
    """将字符串转化为casbin规则"""
    field_values = permission.split("#")
    # 如果长度少于5,则补充为5个
    if len(field_values) < 5:
        field_values.extend([""] * (5 - len(field_values)))
    return field_values


async def casbin_get_subject_permissions(enforcer: Enforcer, subject: str, implicit: bool = False) -> List[str]:
    """根据指定subject主体获取casbin规则"""
    if implicit:
        permissions = await enforcer.get_implicit_permissions_for_user(subject)
    else:
        permissions = await enforcer.get_permissions_for_user(subject)
    return [casbin_permission_encode(*permission[1:]) for permission in permissions]


async def casbin_update_subject_roles(enforcer: Enforcer, subject: str, role_keys: str = None):
    """更新casbin主体权限角色"""
    new_roles = {(subject, "r:" + role) for role in role_keys.split(",") if role}
    # 1. 对比旧的角色,只操作差异部分
    # roles = await enforcer.get_filtered_named_grouping_policy(0,subject)
    # old_roles = {tuple(role) for role in roles}
    # remove_roles = old_roles - new_roles
    # add_roles = new_roles - old_roles
    # if remove_roles:  # 删除旧的资源角色
    #     await enforcer.remove_named_grouping_policies("g", remove_roles)
    # if add_roles:  # 添加新的资源角色
    #     await enforcer.add_named_grouping_policies("g", add_roles)
    # 2.删除全部旧的角色,添加全部新的角色
    await enforcer.delete_roles_for_user(subject)
    if new_roles:
        await enforcer.add_grouping_policies(new_roles)


async def casbin_update_subject_permissions(enforcer: Enforcer, subject: str, permissions: List[str]) -> List[str]:
    """根据指定subject主体更新casbin规则,会删除旧的规则,添加新的规则"""
    old_rules = await enforcer.get_permissions_for_user(subject)
    old_rules = {tuple(i) for i in old_rules}
    # 添加新的权限
    new_rules = set()
    for permission in permissions:
        perm = casbin_permission_decode(permission)
        new_rules.add((subject, *perm))

    remove_rules = old_rules - new_rules
    add_rules = new_rules - old_rules
    if remove_rules:
        # 删除旧的权限
        await enforcer.remove_policies(remove_rules)
    if add_rules:
        await enforcer.add_policies(add_rules)
    return permissions


# 获取全部admin上下级关系
def get_admin_grouping(group: AdminGroup) -> List[Tuple[str, str]]:
    children = []
    for admin in group:
        if admin is admin.app:
            continue
        children.append((admin.app.unique_id, admin.unique_id))
        if isinstance(admin, AdminGroup):
            children.extend(get_admin_grouping(admin))
    return children


# 更新casbin admin资源角色关系
async def casbin_update_site_grouping(enforcer: Enforcer, site: BaseAdminSite):
    """更新casbin admin资源角色关系"""
    roles = await enforcer.get_filtered_named_grouping_policy("g2", 0)
    old_roles = {tuple(role) for role in roles}
    new_roles = set(get_admin_grouping(site))
    remove_roles = old_roles - new_roles
    add_roles = new_roles - old_roles
    if remove_roles:  # 删除旧的资源角色
        await enforcer.remove_named_grouping_policies("g2", remove_roles)
    if add_roles:  # 添加新的资源角色
        await enforcer.add_named_grouping_policies("g2", add_roles)
