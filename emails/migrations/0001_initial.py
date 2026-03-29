from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="EmailQueue",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action_name", models.CharField(blank=True, max_length=128, null=True)),
                ("priority", models.CharField(choices=[("LOW", "Low"), ("NORMAL", "Normal"), ("HIGH", "High"), ("URGENT", "Urgent")], default="NORMAL", max_length=16)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("PROCESSING", "Processing"), ("SENT", "Sent"), ("FAILED", "Failed"), ("DELIVERED", "Delivered"), ("BOUNCED", "Bounced"), ("OPENED", "Opened"), ("CLICKED", "Clicked"), ("COMPLAINED", "Complained"), ("DELIVERY_DELAYED", "Delivery delayed")], default="PENDING", max_length=32)),
                ("from_email", models.EmailField(max_length=254)),
                ("to_email", models.EmailField(max_length=254)),
                ("subject", models.CharField(max_length=255)),
                ("content", models.JSONField(blank=True, default=dict)),
                ("retries", models.PositiveIntegerField(default=0)),
                ("max_retries", models.PositiveIntegerField(default=3)),
                ("error", models.TextField(blank=True, null=True)),
                ("sent_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "db_table": "email_queue",
            },
        ),
        migrations.AddIndex(
            model_name="emailqueue",
            index=models.Index(fields=["priority", "status", "created_at"], name="emailq_pri_st_created_idx"),
        ),
        migrations.AddIndex(
            model_name="emailqueue",
            index=models.Index(fields=["status", "created_at"], name="emailq_st_created_idx"),
        ),
        migrations.AddIndex(
            model_name="emailqueue",
            index=models.Index(fields=["created_at"], name="emailq_created_idx"),
        ),
    ]

