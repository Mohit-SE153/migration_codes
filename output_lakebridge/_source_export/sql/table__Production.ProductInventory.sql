CREATE TABLE [Production].[ProductInventory] (
    [ProductID] int NOT NULL,
    [LocationID] smallint NOT NULL,
    [Shelf] nvarchar(10) NOT NULL,
    [Bin] tinyint NOT NULL,
    [Quantity] smallint NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
