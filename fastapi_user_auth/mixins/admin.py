from datetime import datetime
from functools import cached_property
from typing import Any, Callable, Dict, List, Optional, Set, Union

from fastapi_amis_admin import admin
from fastapi_amis_admin.admin import AdminAction, AdminApp
from fastapi_amis_admin.amis import FormItem, SchemaNode, TableColumn, TableCRUD
from fastapi_amis_admin.crud.base import ItemListSchema, SchemaCreateT, SchemaFilterT, SchemaModelT, SchemaReadT, SchemaUpdateT
from fastapi_amis_admin.crud.schema import BaseApiOut, CrudEnum
from fastapi_amis_admin.utils.pydantic import ModelField
from sqlalchemy.engine import Result
from starlette.requests import Request

from fastapi_user_auth.auth.schemas import SystemUserEnum
from fastapi_user_auth.mixins.schemas import PermissionExcludeDict
from fastapi_user_auth.utils import get_schema_fields_name_label


class ReadOnlyModelAdmin(admin.ModelAdmin):
    """只读模型管理Mixin
    移除了创建,更新,删除等操作
    """

    @cached_property
    def registered_admin_actions(self) -> Dict[str, "AdminAction"]:
        actions = super().registered_admin_actions
        return {
            key: action
            for key, action in actions.items()
            if key not in {"create", "update", "delete", "bulk_delete", "bulk_update", "bulk_create"}
        }

    async def has_create_permission(self, request: Request, data: SchemaUpdateT, **kwargs) -> bool:
        return False

    async def has_update_permission(
        self,
        request: Request,
        item_id: List[str],
        data: SchemaUpdateT,
        **kwargs,
    ) -> bool:
        return False

    async def has_delete_permission(self, request: Request, item_id: List[str], **kwargs) -> bool:
        return False


class AutoTimeModelAdmin(admin.ModelAdmin):
    """禁止修改时间Mixin,没有Id,创建时间,更新时间,删除时间等字段的创建和更新"""

    create_exclude = {
        "id",
        "create_time",
        "update_time",
        "delete_time",
    }
    update_exclude = {
        "id",
        "create_time",
        "update_time",
        "delete_time",
    }


class SoftDeleteModelAdmin(AutoTimeModelAdmin):
    """软删除模型Mixin.
    - 需要在模型中定义delete_time字段.如果delete_time字段为None,则表示未删除.
    """

    def __init__(self, app: "AdminApp"):
        super().__init__(app)
        assert hasattr(self.model, "delete_time"), "SoftDeleteAdminMixin需要在模型中定义delete_time字段"

    async def get_select(self, request: Request):
        sel = await super().get_select(request)
        return sel.where(self.model.delete_time == None)  # noqa E711

    def delete_item(self, obj: SchemaModelT) -> None:
        obj.delete_time = datetime.now()


class FootableModelAdmin(admin.ModelAdmin):
    """为模型管理Amis表格添加底部展示(Footable)属性"""

    async def get_list_table(self, request: Request) -> TableCRUD:
        table = await super().get_list_table(request)
        table.footable = True
        return table


class AuthModelAdmin(admin.ModelAdmin):
    """字段级别权限控制模型管理.
    - xxx_permission_fields:
        1.动作权限字段,可以通过覆盖这些属性来控制哪些字段需要进行权限验证.
        2.未设置的字段,则不进行权限验证.
        3.一旦类被实例化,则会缓存这些属性,禁止再次修改.
    #todo  初步实现,未优化
    """

    permission_exclude: PermissionExcludeDict = None

    def __init__(self, app: "AdminApp"):
        super().__init__(app)
        self.permission_exclude = self.permission_exclude or {}

    def get_permission_fields(self, action: str) -> Dict[str, str]:
        """获取权限字段"""
        all_exclude = self.permission_exclude.get("all", [])
        if action == "list":
            list_exclude = self.permission_exclude.get("list", [])
            return get_schema_fields_name_label(
                self.schema_list, prefix="列表展示-", exclude_required=True, exclude=[*all_exclude, *list_exclude]
            )
        elif action == "filter":
            filter_exclude = self.permission_exclude.get("filter", [])
            return get_schema_fields_name_label(
                self.schema_filter, prefix="列表筛选-", exclude_required=True, exclude=[*all_exclude, *filter_exclude]
            )
        elif action == "create":
            create_exclude = self.permission_exclude.get("create", [])
            return get_schema_fields_name_label(
                self.schema_create, prefix="新增-", exclude_required=True, exclude=[*all_exclude, *create_exclude]
            )
        elif action == "read":
            read_exclude = self.permission_exclude.get("read", [])
            return get_schema_fields_name_label(
                self.schema_read, prefix="查看-", exclude_required=True, exclude=[*all_exclude, *read_exclude]
            )
        elif action == "update":
            update_exclude = self.permission_exclude.get("update", [])
            return get_schema_fields_name_label(
                self.schema_update, prefix="更新-", exclude_required=True, exclude=[*all_exclude, *update_exclude]
            )
        return {}

    @cached_property
    def create_permission_fields(self) -> Dict[str, str]:
        """创建权限字段"""
        return self.get_permission_fields("create")

    @cached_property
    def read_permission_fields(self) -> Dict[str, str]:
        """读取权限字段"""
        return self.get_permission_fields("read")

    @cached_property
    def update_permission_fields(self) -> Dict[str, str]:
        """更新权限字段"""
        return self.get_permission_fields("update")

    @cached_property
    def list_permission_fields(self) -> Dict[str, str]:
        """列表权限字段"""
        return self.get_permission_fields("list")

    @cached_property
    def filter_permission_fields(self) -> Dict[str, str]:
        """过滤筛选权限字段"""
        return self.get_permission_fields("filter")

    async def has_field_permission(self, request: Request, field: str, action: str = None) -> bool:
        """判断用户是否有字段权限"""
        subject = await self.site.auth.get_current_user_identity(request) or SystemUserEnum.GUEST
        effect = self.site.auth.enforcer.enforce("u:" + subject, self.unique_id, f"page:{action}:{field}", f"page:{action}")
        return effect

    async def get_deny_fields(self, request: Request, action: str = None) -> Set[str]:
        """获取没有权限的字段"""
        cache_key = f"{self.unique_id}_exclude_fields"
        request_cache = request.scope.get(cache_key, {})
        if action in request_cache:
            return request_cache[action]
        check_fields = {}
        if action == "list":
            check_fields = self.list_permission_fields.keys()
        elif action == "filter":
            check_fields = self.filter_permission_fields.keys()
        elif action == "create":
            check_fields = self.create_permission_fields.keys()
        elif action == "update":
            check_fields = self.update_permission_fields.keys()
        elif action == "read":
            check_fields = self.read_permission_fields.keys()
        else:
            pass
        fields = {field for field in check_fields if not await self.has_field_permission(request, field, action)}
        request_cache[action] = fields
        if cache_key not in request.scope:
            request.scope[f"{self.unique_id}_exclude_fields"] = request_cache
        return fields

    async def on_list_after(self, request: Request, result: Result, data: ItemListSchema, **kwargs) -> ItemListSchema:
        """Parse the database data query result dictionary into schema_list."""
        exclude = await self.get_deny_fields(request, "list")  # 过滤没有权限的字段
        data = await super().on_list_after(request, result, data, **kwargs)
        data.items = [item.dict(exclude=exclude) for item in data.items]  # 过滤没有权限的字段
        return data

    async def on_filter_pre(self, request: Request, obj: Optional[SchemaFilterT], **kwargs) -> Dict[str, Any]:
        data = await super().on_filter_pre(request, obj, **kwargs)
        if not data:
            return data
        exclude = await self.get_deny_fields(request, "filter")  # 过滤没有权限的字段
        return {k: v for k, v in data.items() if k not in exclude}

    async def on_create_pre(self, request: Request, obj: SchemaCreateT, **kwargs) -> Dict[str, Any]:
        exclude = await self.get_deny_fields(request, "create")  # 过滤没有权限的字段
        obj = obj.copy(exclude=exclude)  # 过滤没有权限的字段
        data = await super().on_create_pre(request, obj, **kwargs)
        return data

    async def on_update_pre(
        self,
        request: Request,
        obj: SchemaUpdateT,
        item_id: Union[List[str], List[int]],
        **kwargs,
    ) -> Dict[str, Any]:
        exclude = await self.get_deny_fields(request, "update")  # 过滤没有权限的字段
        obj = obj.copy(exclude=exclude)  # 过滤没有权限的字段
        data = await super().on_update_pre(request, obj, item_id, **kwargs)
        return data

    async def on_read_after(
        self,
        request: Request,
        obj: SchemaReadT,
    ):
        exclude = await self.get_deny_fields(request, "read")  # 过滤没有权限的字段
        obj = obj.copy(exclude=exclude)  # 过滤没有权限的字段
        return obj

    @property
    def route_read(self) -> Callable:
        async def route(
            request: Request,
            item_id: self.AnnotatedItemIdList,  # type: ignore
        ):
            if not await self.has_read_permission(request, item_id):
                return self.error_no_router_permission(request)
            items = await self.db.async_run_sync(self._read_items, item_id)
            items = [await self.on_read_after(request, item) for item in items]
            if len(items) == 1:
                items = items[0]
            return BaseApiOut(data=items)

        return route

    async def get_form_item(
        self, request: Request, modelfield: ModelField, action: CrudEnum
    ) -> Union[FormItem, SchemaNode, None]:
        """过滤前端创建,更新,筛选表单字段"""
        # todo 优化筛选和列表动作的界定
        # action为list时,表示列表展示字段.否则为筛选表单字段
        act = "filter" if action == "list" else action
        exclude = await self.get_deny_fields(request, act)  # 获取没有权限的字段
        name = modelfield.alias or modelfield.name
        if name in exclude:
            return None
        form_item = await super().get_form_item(request, modelfield, action)
        return form_item

    async def get_list_column(self, request: Request, modelfield: ModelField) -> TableColumn:
        """过滤前端展示字段"""
        exclude = await self.get_deny_fields(request, "list")  # 获取没有权限的字段
        name = modelfield.alias or modelfield.name
        if name in exclude:
            return None
        column = await super().get_list_column(request, modelfield)
        return column


class AuthFormAdmin(admin.FormAdmin):
    """#todo 字段级别权限控制表单管理"""

    pass
