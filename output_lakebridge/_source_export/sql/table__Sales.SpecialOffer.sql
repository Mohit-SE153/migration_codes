CREATE TABLE [Sales].[SpecialOffer] (
    [SpecialOfferID] int NOT NULL,
    [Description] nvarchar(255) NOT NULL,
    [DiscountPct] smallmoney NOT NULL,
    [Type] nvarchar(50) NOT NULL,
    [Category] nvarchar(50) NOT NULL,
    [StartDate] datetime NOT NULL,
    [EndDate] datetime NOT NULL,
    [MinQty] int NOT NULL,
    [MaxQty] int NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
