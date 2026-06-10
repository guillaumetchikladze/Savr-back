from urllib.parse import parse_qs

from channels.generic.websocket import AsyncJsonWebsocketConsumer

from core.ws_auth import get_user_from_token


class ShoppingListConsumer(AsyncJsonWebsocketConsumer):
    """
    WebSocket consumer for real-time shopping list updates.

    - Authenticates via JWT access token provided as ?token=<JWT>.
    - Supports "join_list" / "leave_list" actions to subscribe to a given shopping list room.
    - For each shopping list, a group "shopping_list_<id>" is used.
    """

    async def connect(self):
        # Expect a JWT access token in the query string (?token=...)
        query_string = self.scope.get("query_string", b"").decode("utf-8")
        query_params = parse_qs(query_string)
        tokens = query_params.get("token") or []
        token = tokens[0] if tokens else None

        if not token:
            await self.close(code=4001)
            return

        user = await get_user_from_token(token)
        if not user:
            await self.close(code=4003)
            return

        # Attach user to the scope for later use
        self.scope["user"] = user
        self.list_groups = set()

        await self.accept()

    async def disconnect(self, close_code):
        # Remove this channel from all joined groups
        for group in list(self.list_groups):
            await self.channel_layer.group_discard(group, self.channel_name)
        self.list_groups.clear()

    async def receive_json(self, content, **kwargs):
        """
        Handle client -> server messages.
        Expected payloads:
        - {"action": "join_list", "list_id": 123}
        - {"action": "leave_list", "list_id": 123}
        """
        action = content.get("action")
        list_id = content.get("list_id")

        if not action or list_id is None:
            return

        group_name = f"shopping_list_{list_id}"

        if action == "join_list":
            await self.channel_layer.group_add(group_name, self.channel_name)
            self.list_groups.add(group_name)
        elif action == "leave_list":
            await self.channel_layer.group_discard(group_name, self.channel_name)
            self.list_groups.discard(group_name)

    # Handlers for events coming from the channel layer
    async def shopping_list_item_updated(self, event):
        """
        Broadcast a shopping list item update to the client.
        """
        await self.send_json(
            {
                "type": "shopping_list_item_updated",
                "item": event.get("item"),
                "updated_by_user_id": event.get("updated_by_user_id"),
            }
        )

    async def shopping_list_conflict_notice(self, event):
        """
        Broadcast a conflict notice (offline sync conflict) to the client.
        """
        await self.send_json(
            {
                "type": "shopping_list_conflict_notice",
                "shopping_list_id": event.get("shopping_list_id"),
                "actor_user_id": event.get("actor_user_id"),
                "actor_username": event.get("actor_username"),
                "target_user_ids": event.get("target_user_ids") or [],
                "items": event.get("items") or [],
            }
        )

