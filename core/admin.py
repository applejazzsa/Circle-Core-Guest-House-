from django.contrib import admin

from .models import (
    AuditLog,
    Booking,
    BookingRefund,
    CommunicationLog,
    CleaningProof,
    DailyCloseLock,
    Expense,
    Guest,
    GuestHouseSettings,
    InventoryCategory,
    InventoryItem,
    InventoryTransaction,
    MaintenanceRequest,
    Payment,
    RatePlan,
    Room,
    RoomAllocation,
    RoomInventoryAssignment,
    RoomType,
    StaffProfile,
    Subscription,
    SubscriptionPlan,
    TrialLicense,
)


@admin.register(StaffProfile)
class StaffProfileAdmin(admin.ModelAdmin):
    list_display = ["user", "phone_number", "role", "pin_enabled", "pin_failed_attempts", "pin_locked_until"]
    list_filter = ["role", "pin_enabled"]
    search_fields = ["user__username", "user__email", "phone_number"]
    readonly_fields = ["pin_failed_attempts", "pin_locked_until", "updated_at"]
    fields = ["user", "phone_number", "role", "pin_enabled", "pin_failed_attempts", "pin_locked_until", "updated_at"]


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "actor", "action", "object_type", "object_repr", "approved_by", "ip_address"]
    list_filter = ["action", "object_type", "created_at"]
    search_fields = ["object_repr", "reason", "actor__username", "approved_by__username"]
    readonly_fields = ["created_at", "actor", "approved_by", "action", "object_type", "object_id", "object_repr", "before", "after", "reason", "ip_address"]
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(CleaningProof)
class CleaningProofAdmin(admin.ModelAdmin):
    list_display = ["created_at", "room", "uploaded_by", "notes"]
    list_filter = ["created_at", "room"]
    search_fields = ["room__name", "uploaded_by__username", "notes"]
    readonly_fields = ["created_at"]


@admin.register(DailyCloseLock)
class DailyCloseLockAdmin(admin.ModelAdmin):
    list_display = ["close_date", "locked_by", "locked_at"]
    search_fields = ["notes", "locked_by__username"]
    readonly_fields = ["locked_at"]


@admin.register(CommunicationLog)
class CommunicationLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "channel", "template_name", "recipient", "status", "created_by"]
    list_filter = ["channel", "status", "created_at"]
    search_fields = ["recipient", "message", "booking__booking_reference", "guest__first_name", "guest__last_name"]
    readonly_fields = ["created_at"]


@admin.register(GuestHouseSettings)
class GuestHouseSettingsAdmin(admin.ModelAdmin):
    fieldsets = (
        (
            "Guest House",
            {
                "fields": (
                    "guest_house_name",
                    "logo",
                    "phone",
                    "email",
                    "address",
                )
            },
        ),
        (
            "Financial",
            {
                "fields": (
                    "banking_details",
                    "vat_registered",
                    "vat_number",
                    "vat_rate",
                    "currency",
                )
            },
        ),
        (
            "Operations",
            {
                "fields": (
                    "check_in_time",
                    "check_out_time",
                    "cancellation_note",
                    "invoice_notes",
                    "receipt_notes",
                    "enable_hourly_bookings",
                    "enable_weekly_bookings",
                    "hourly_booking_label",
                    "minimum_hourly_rate",
                    "default_price_2_hours",
                    "default_price_3_hours",
                    "default_price_per_night",
                    "default_price_24_hours",
                    "late_checkout_fee",
                    "early_checkin_fee",
                    "weekend_surcharge_pct",
                    "seasonal_note",
                )
            },
        ),
    )

    def has_add_permission(self, request):
        if GuestHouseSettings.objects.exists():
            return False
        return super().has_add_permission(request)


@admin.register(Guest)
class GuestAdmin(admin.ModelAdmin):
    list_display = ["full_name", "phone", "email", "created_at"]
    search_fields = ["first_name", "last_name", "phone", "email", "id_passport_number"]
    list_filter = ["created_at"]
    readonly_fields = ["created_at"]


@admin.register(RatePlan)
class RatePlanAdmin(admin.ModelAdmin):
    list_display = ["name", "amount", "currency", "pricing_basis", "is_active", "updated_at"]
    list_filter = ["pricing_basis", "is_active", "currency"]
    search_fields = ["name", "description"]
    actions = ["reapply_to_linked_rooms"]

    @admin.action(description="Reapply rate to all linked rooms")
    def reapply_to_linked_rooms(self, request, queryset):
        for rate_plan in queryset:
            rate_plan.apply_to_rooms()


@admin.register(RoomType)
class RoomTypeAdmin(admin.ModelAdmin):
    list_display = [
        "name", "bathroom_type", "gender_restriction", "is_wheelchair_accessible",
        "area", "default_rate_plan", "is_active",
    ]
    list_filter = ["bathroom_type", "gender_restriction", "is_wheelchair_accessible", "is_active"]
    search_fields = ["name", "area", "bathroom_description"]


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = [
        "name", "room_type", "room_category", "rate_plan", "booking_mode",
        "price_per_night", "price_per_week", "max_guests", "status", "cleaning_status",
    ]
    list_filter = ["room_type", "room_category", "rate_plan", "booking_mode", "status", "cleaning_status"]
    search_fields = ["name", "description"]
    list_editable = ["status", "cleaning_status"]


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ["payment_date", "payment_type", "payment_method", "amount", "reference"]


class RoomAllocationInline(admin.TabularInline):
    model = RoomAllocation
    extra = 0
    fields = ["room", "allocated_guests", "rate_plan", "rate_per_night", "line_total"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(Booking)
class BookingAdmin(admin.ModelAdmin):
    list_display = [
        "booking_reference", "guest", "room",
        "check_in_date", "check_out_date", "booking_duration_type", "status", "total_amount", "balance_due",
    ]
    list_filter = ["status", "booking_source", "check_in_date"]
    search_fields = [
        "booking_reference",
        "guest__first_name", "guest__last_name",
        "room__name",
    ]
    readonly_fields = ["booking_reference", "total_amount", "balance_due", "created_at"]
    date_hierarchy = "check_in_date"
    inlines = [PaymentInline, RoomAllocationInline]


@admin.register(RoomAllocation)
class RoomAllocationAdmin(admin.ModelAdmin):
    list_display = ["booking", "room", "allocated_guests", "rate_plan", "rate_per_night", "line_total", "created_at"]
    list_filter = ["rate_plan", "room"]
    search_fields = ["booking__booking_reference", "room__name"]
    readonly_fields = ["created_at", "updated_at"]


@admin.register(TrialLicense)
class TrialLicenseAdmin(admin.ModelAdmin):
    list_display = ["license_key", "guest_house_name", "owner_email", "expires_at", "is_active", "converted_to_paid"]
    list_filter = ["is_active", "converted_to_paid", "trial_extended"]
    search_fields = ["license_key", "guest_house_name", "owner_email"]


@admin.register(SubscriptionPlan)
class SubscriptionPlanAdmin(admin.ModelAdmin):
    list_display = ["display_name", "name", "monthly_price", "annual_price", "max_rooms", "max_users"]
    list_filter = ["name"]
    readonly_fields = ["feature_summary"]

    def feature_summary(self, obj):
        features = [
            "expenses",
            "full_reports",
            "export",
            "hourly_bookings",
            "weekly_bookings",
            "inventory",
            "staff_roles",
            "multi_property",
            "api_access",
            "custom_pdf_branding",
            "maintenance_requests",
            "priority_support",
        ]
        return ", ".join(feature.replace("_", " ").title() for feature in features if getattr(obj, f"feature_{feature}", False)) or "No premium features"


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ["plan", "status", "owner_email", "days_remaining", "expires_at"]
    list_filter = ["status", "plan", "billing_cycle", "expires_at"]
    search_fields = ["owner_name", "owner_email", "owner_phone", "last_payment_reference"]
    readonly_fields = ["started_at", "days_remaining", "plan_features"]
    actions = ["extend_by_30_days", "mark_as_active", "mark_as_expired"]
    fieldsets = (
        ("Subscription", {"fields": ("plan", "billing_cycle", "status", "started_at", "expires_at", "trial_ends_at", "auto_renew", "plan_features")}),
        ("Owner", {"fields": ("owner_name", "owner_email", "owner_phone")}),
        ("Billing", {"fields": ("last_payment_date", "last_payment_amount", "last_payment_reference", "next_billing_date")}),
        ("Notes", {"fields": ("notes",)}),
    )

    def plan_features(self, obj):
        if not obj or not obj.plan_id:
            return "-"
        features = [
            "expenses",
            "full_reports",
            "export",
            "hourly_bookings",
            "weekly_bookings",
            "inventory",
            "staff_roles",
            "multi_property",
            "api_access",
            "custom_pdf_branding",
            "maintenance_requests",
            "priority_support",
        ]
        return ", ".join(feature.replace("_", " ").title() for feature in features if obj.has_feature(feature)) or "No premium features"

    @admin.action(description="Renew next billing period")
    def extend_by_30_days(self, request, queryset):
        from core.subscriptions import apply_paid_renewal

        for subscription in queryset:
            apply_paid_renewal(subscription)
            subscription.save(update_fields=["status", "trial_ends_at", "expires_at", "next_billing_date"])
        self.message_user(request, "Selected subscriptions renewed for their next billing period.")

    @admin.action(description="Mark as Active")
    def mark_as_active(self, request, queryset):
        queryset.update(status="active")
        self.message_user(request, "Selected subscriptions marked active.")

    @admin.action(description="Mark as Expired")
    def mark_as_expired(self, request, queryset):
        queryset.update(status="expired")
        self.message_user(request, "Selected subscriptions marked expired.")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = [
        "booking",
        "payment_date",
        "payment_type",
        "payment_method",
        "amount",
        "reference",
        "recorded_at",
    ]
    list_filter = ["payment_method", "payment_type", "payment_date"]
    search_fields = [
        "booking__booking_reference",
        "booking__guest__first_name",
        "booking__guest__last_name",
        "reference",
    ]
    readonly_fields = ["recorded_at"]
    date_hierarchy = "payment_date"


@admin.register(BookingRefund)
class BookingRefundAdmin(admin.ModelAdmin):
    list_display = ["booking", "refund_date", "refund_method", "amount", "reference", "approved_by", "recorded_by", "recorded_at"]
    list_filter = ["refund_method", "refund_date", "recorded_at"]
    search_fields = ["booking__booking_reference", "booking__guest__first_name", "booking__guest__last_name", "reference", "reason"]
    readonly_fields = ["recorded_at"]
    date_hierarchy = "refund_date"


@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = ["date", "category", "description", "amount", "paid_to", "payment_method"]
    list_filter = ["category", "payment_method", "date"]
    search_fields = ["description", "paid_to", "reference", "notes"]
    readonly_fields = ["created_at"]
    date_hierarchy = "date"


@admin.register(MaintenanceRequest)
class MaintenanceRequestAdmin(admin.ModelAdmin):
    list_display = ["title", "room", "category", "priority", "status", "assigned_to", "scheduled_for", "reported_at"]
    list_filter = ["status", "priority", "category", "scheduled_for", "reported_at"]
    search_fields = ["title", "description", "room__name", "assigned_to", "assigned_phone"]
    readonly_fields = ["reported_at", "resolved_at"]
    date_hierarchy = "reported_at"


@admin.register(InventoryCategory)
class InventoryCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "color", "description"]
    search_fields = ["name", "description"]


class RoomInventoryAssignmentInline(admin.TabularInline):
    model = RoomInventoryAssignment
    extra = 0


@admin.register(InventoryItem)
class InventoryItemAdmin(admin.ModelAdmin):
    list_display = ["name", "category", "current_stock", "minimum_stock", "unit", "location", "is_active"]
    list_filter = ["category", "location", "is_active"]
    search_fields = ["name", "sku", "supplier_name", "location"]
    readonly_fields = ["created_at"]


@admin.register(InventoryTransaction)
class InventoryTransactionAdmin(admin.ModelAdmin):
    list_display = ["recorded_at", "item", "transaction_type", "quantity", "stock_before", "stock_after", "room", "recorded_by"]
    list_filter = ["transaction_type", "recorded_at", "item__category"]
    search_fields = ["item__name", "reference", "notes", "room__name"]
    readonly_fields = ["recorded_at", "stock_before", "stock_after"]


@admin.register(RoomInventoryAssignment)
class RoomInventoryAssignmentAdmin(admin.ModelAdmin):
    list_display = ["room", "item", "expected_quantity"]
    list_filter = ["room", "item__category"]
    search_fields = ["room__name", "item__name", "notes"]
