from rest_framework import serializers

from .models import PushDevice


class PushDeviceRegisterSerializer(serializers.Serializer):
  expo_push_token = serializers.CharField(max_length=255)
  platform = serializers.ChoiceField(choices=['android', 'ios'])

  def save(self, **kwargs):
    user = self.context['request'].user
    token = self.validated_data['expo_push_token']
    platform = self.validated_data['platform']

    device, _created = PushDevice.objects.update_or_create(
      expo_push_token=token,
      defaults={
        'user': user,
        'platform': platform,
        'is_active': True,
      },
    )
    return device

